"""MCP 서버와 웹 데모가 함께 쓰는 노트 저장소 (JSON 파일)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

STORE = Path(__file__).resolve().parent.parent / "data" / "mcp_notes.json"

SEED = [
    {"id": 1, "title": "Q4 로드맵", "body": "RAG 검색 품질 개선, 에이전트 평가 자동화, 비용 대시보드 출시", "tags": ["roadmap", "ai"]},
    {"id": 2, "title": "장애 회고 2026-09-12", "body": "임베딩 배치 작업이 레이트리밋에 걸림. 지수 백오프와 큐 도입으로 해결", "tags": ["incident"]},
    {"id": 3, "title": "면접 질문 아이디어", "body": "프롬프트 인젝션 방어 전략, eval 설계 경험, 토큰 비용 최적화 사례", "tags": ["hiring"]},
]


def _load() -> list[dict]:
    if not STORE.exists():
        _save([dict(n, created_at=datetime.now().isoformat(timespec="seconds")) for n in SEED])
    return json.loads(STORE.read_text(encoding="utf-8"))


def _save(notes: list[dict]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")


def list_notes(tag: str | None = None) -> list[dict]:
    notes = _load()
    return [n for n in notes if not tag or tag in n["tags"]]


def search_notes(query: str) -> list[dict]:
    terms = [t for t in re.split(r"\s+", query.lower()) if t]
    scored = []
    for n in _load():
        hay = f"{n['title']} {n['body']} {' '.join(n['tags'])}".lower()
        score = sum(hay.count(t) for t in terms)
        if score:
            scored.append((score, n))
    return [n for _, n in sorted(scored, key=lambda x: -x[0])]


def add_note(title: str, body: str, tags: list[str] | None = None) -> dict:
    notes = _load()
    note = {"id": max((n["id"] for n in notes), default=0) + 1, "title": title, "body": body,
            "tags": tags or [], "created_at": datetime.now().isoformat(timespec="seconds")}
    notes.append(note)
    _save(notes)
    return note


def delete_note(note_id: int) -> bool:
    notes = _load()
    rest = [n for n in notes if n["id"] != note_id]
    _save(rest)
    return len(rest) != len(notes)
