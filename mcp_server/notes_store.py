"""MCP 서버와 웹 데모가 함께 쓰는 노트 저장소 (JSON 파일).

- 기본 저장소: data/mcp_notes.json (환경변수 NOTES_STORE로 변경). Claude Desktop / Claude Code에서 쓰는 파일.
- 웹 데모: 방문자별 샌드박스 파일 data/mcp_sessions/<32자리 hex>.json — 한 방문자의 추가·삭제가 다른 방문자에게
  보이지 않는다 (공개 데모의 낙서·시드 삭제·저장형 프롬프트 인젝션 방지). 최근 MAX_SANDBOXES개만 남긴다.
- 동시성: 같은 파일에 여러 프로세스(MCP 서버, Claude Desktop)·스레드가 접근해도 깨지지 않도록
  공개 함수마다 잠금을 한 번 잡고(읽기 포함), 저장은 임시 파일에 쓴 뒤 os.replace로 원자적으로 교체한다.
- 입력 제한 위반은 ValueError로 알린다. 이 모듈은 mcp에 의존하지 않고, MCP 계층(server.py)이 ToolError로 바꾼다.
"""
from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

DATA = Path(__file__).resolve().parent.parent / "data"
STORE = Path(os.getenv("NOTES_STORE") or DATA / "mcp_notes.json")
SANDBOX_DIR = Path(os.getenv("NOTES_SANDBOX_DIR") or DATA / "mcp_sessions")
SANDBOX_META_KEY = "team-notes/sandbox"  # MCP 요청 _meta 키 (projects/p09_mcp.py와 같은 값)
MAX_SANDBOXES = 100

# 공개 데모 비용·용량 상한: 노트 내용은 그대로 Claude 입력 토큰이 된다
MAX_NOTES = 30
MAX_TITLE = 100
MAX_BODY = 2000
MAX_TAGS = 5
MAX_TAG_LEN = 30
MAX_QUERY = 200

SEED = [
    {"id": 1, "title": "Q4 로드맵", "body": "RAG 검색 품질 개선, 에이전트 평가 자동화, 비용 대시보드 출시", "tags": ["roadmap", "ai"]},
    {"id": 2, "title": "장애 회고 2026-09-12", "body": "임베딩 배치 작업이 레이트리밋에 걸림. 지수 백오프와 큐 도입으로 해결", "tags": ["incident"]},
    {"id": 3, "title": "면접 질문 아이디어", "body": "프롬프트 인젝션 방어 전략, eval 설계 경험, 토큰 비용 최적화 사례", "tags": ["hiring"]},
]

_SANDBOX_ID = re.compile(r"[0-9a-f]{32}")
_thread_lock = threading.Lock()  # 같은 프로세스 안의 스레드끼리 (파일 잠금 재시도 대기 방지)


def store_for(sandbox: str | None) -> Path:
    """샌드박스 id → 저장 파일. None/빈 값이면 기본 저장소. 형식을 강제해 경로 조작을 막는다."""
    if not sandbox:
        return STORE
    if not isinstance(sandbox, str) or not _SANDBOX_ID.fullmatch(sandbox):
        raise ValueError("잘못된 샌드박스 id입니다.")
    return SANDBOX_DIR / f"{sandbox}.json"


# ---- 잠금 / 원자적 저장 -------------------------------------------------------------------------

def _lock_file(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # 최대 10초 재시도
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock_file(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _ensure_dir(directory: Path) -> None:
    if directory.is_dir():
        return
    directory.mkdir(parents=True, exist_ok=True)
    if directory == SANDBOX_DIR:  # 런타임 데이터 — 저장소에 커밋되지 않게
        (directory / ".gitignore").write_text("*\n", encoding="utf-8")


@contextmanager
def _locked(store: Path) -> Iterator[None]:
    """store에 대한 배타 잠금. 재진입 불가 — 공개 함수에서 정확히 한 번만 잡는다."""
    _ensure_dir(store.parent)
    with _thread_lock, open(store.with_suffix(".lock"), "a+b") as fh:
        _lock_file(fh)
        try:
            yield
        finally:
            _unlock_file(fh)


def _seed() -> list[dict]:
    now = datetime.now().isoformat(timespec="seconds")
    return [dict(n, tags=list(n["tags"]), created_at=now) for n in SEED]


def _save(store: Path, notes: list[dict]) -> None:  # 호출자가 _locked(store)를 잡고 있어야 한다
    tmp = store.with_name(f"{store.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, store)


def _load_locked(store: Path) -> list[dict]:  # 호출자가 _locked(store)를 잡고 있어야 한다
    try:
        notes = json.loads(store.read_text(encoding="utf-8"))
        if isinstance(notes, list):
            return notes
        raise ValueError("노트 목록 형식이 아닙니다")
    except FileNotFoundError:
        pass
    except (ValueError, OSError):  # 깨진 파일: 백업해 두고 시드로 복구 (데모가 계속 동작하도록)
        try:
            os.replace(store, store.with_name(store.name + ".corrupt"))
        except OSError:
            pass
    seeded = _seed()
    _save(store, seeded)
    if store.parent == SANDBOX_DIR:
        _prune_sandboxes(keep=store)
    return seeded


def _prune_sandboxes(keep: Path) -> None:
    """최근에 쓴 샌드박스 MAX_SANDBOXES개만 남긴다 (동시 요청과 경합할 수 있어 실패는 무시)."""
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    files = sorted((p for p in SANDBOX_DIR.glob("*.json") if p != keep), key=mtime, reverse=True)
    for p in files[MAX_SANDBOXES - 1:]:
        for q in (p, p.with_suffix(".lock")):
            try:
                q.unlink()
            except OSError:
                pass


def _load(store: Path) -> list[dict]:
    with _locked(store):
        return _load_locked(store)


# ---- 공개 API ------------------------------------------------------------------------------------

def list_notes(tag: str | None = None, *, store: Path | None = None) -> list[dict]:
    notes = _load(store or STORE)
    return [n for n in notes if not tag or tag in n.get("tags", [])]


def search_notes(query: str, *, store: Path | None = None) -> list[dict]:
    if len(query) > MAX_QUERY:
        raise ValueError(f"검색어는 {MAX_QUERY}자 이내로 입력하세요.")
    terms = [t for t in re.split(r"\s+", query.lower()) if t]
    scored = []
    for n in _load(store or STORE):
        hay = f"{n.get('title', '')} {n.get('body', '')} {' '.join(n.get('tags', []))}".lower()
        score = sum(hay.count(t) for t in terms)
        if score:
            scored.append((score, n))
    return [n for _, n in sorted(scored, key=lambda x: -x[0])]


def _clean_tags(tags: list[str] | None) -> list[str]:
    out = list(dict.fromkeys(t.strip() for t in (tags or []) if isinstance(t, str) and t.strip()))
    if len(out) > MAX_TAGS:
        raise ValueError(f"태그는 최대 {MAX_TAGS}개까지 붙일 수 있습니다.")
    if any(len(t) > MAX_TAG_LEN for t in out):
        raise ValueError(f"태그는 {MAX_TAG_LEN}자 이내로 입력하세요.")
    return out


def add_note(title: str, body: str, tags: list[str] | None = None, *, store: Path | None = None) -> dict:
    title, body = (title or "").strip(), (body or "").strip()
    if not title:
        raise ValueError("제목을 입력하세요.")
    if len(title) > MAX_TITLE:
        raise ValueError(f"제목은 {MAX_TITLE}자 이내로 입력하세요 (현재 {len(title)}자).")
    if len(body) > MAX_BODY:
        raise ValueError(f"본문은 {MAX_BODY}자 이내로 입력하세요 (현재 {len(body)}자). 요약해서 다시 시도하세요.")
    tags = _clean_tags(tags)
    store = store or STORE
    with _locked(store):
        notes = _load_locked(store)
        if len(notes) >= MAX_NOTES:
            raise ValueError(f"노트는 최대 {MAX_NOTES}개까지 저장할 수 있습니다. delete_note로 정리한 뒤 다시 시도하세요.")
        note = {"id": max((n["id"] for n in notes), default=0) + 1, "title": title, "body": body,
                "tags": tags, "created_at": datetime.now().isoformat(timespec="seconds")}
        notes.append(note)
        _save(store, notes)
    return note


def delete_note(note_id: int, *, store: Path | None = None) -> bool:
    store = store or STORE
    with _locked(store):
        notes = _load_locked(store)
        rest = [n for n in notes if n["id"] != note_id]
        if len(rest) != len(notes):
            _save(store, rest)
    return len(rest) != len(notes)
