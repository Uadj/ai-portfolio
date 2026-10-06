"""#06 코드 리뷰 봇 — diff 파싱 → 구조화 리뷰 → 라인 검증 → (선택) GitHub PR 인라인 코멘트."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from typing import List, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core import llm

router = APIRouter(prefix="/api/p06")


MAX_DIFF_CHARS = 60_000  # 공개 데모 비용 보호 (~15k 토큰)
log = logging.getLogger(__name__)


# ---------- diff 파서 ----------
def _diff_path(header: str) -> str:
    path = header[4:].split("\t")[0].strip()  # '+++ b/x.py\t2024-01-01 ...' 처럼 붙는 타임스탬프 제거
    return path[2:] if path.startswith(("a/", "b/")) else path


def parse_diff(diff: str) -> list[dict]:
    """unified diff → [{file, hunks:[{header, lines:[{type, old, new, text}]}]}]"""
    files, cur, hunk = [], None, None
    old_no = new_no = 0
    # git 처럼 '\n' 으로만 나눈다 (splitlines 는 코드 속 \f, U+2028 에서도 끊어 줄 번호를 밀어 버림)
    lines = re.split(r"\r?\n", diff.rstrip("\r\n")) if diff.strip() else []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("diff --git"):
            cur = hunk = None
            continue
        # 파일 헤더 = '--- X' / '+++ Y' / '@@' 3줄. 헝크 안에서 '-- '(SQL 주석 삭제)·'++ '(추가)로 시작하는 내용 줄은 헤더가 아니다
        if line.startswith("--- ") and nxt.startswith("+++ ") and i + 2 < len(lines) and lines[i + 2].startswith("@@ "):
            continue
        if line.startswith("+++ ") and i and lines[i - 1].startswith("--- ") and nxt.startswith("@@ "):
            path = _diff_path(line)
            if path == "/dev/null":  # 삭제된 파일은 원래 경로로 표시
                path = _diff_path(lines[i - 1])
            cur, hunk = {"file": path, "hunks": []}, None
            files.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(2))
            hunk = {"header": line, "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None or line.startswith("\\"):  # '\ No newline at end of file' 은 줄이 아님
            continue
        if line.startswith("+"):
            hunk["lines"].append({"type": "add", "old": None, "new": new_no, "text": line[1:]})
            new_no += 1
        elif line.startswith("-"):
            hunk["lines"].append({"type": "del", "old": old_no, "new": None, "text": line[1:]})
            old_no += 1
        else:
            hunk["lines"].append({"type": "ctx", "old": old_no, "new": new_no, "text": line[1:] if line else ""})
            old_no += 1
            new_no += 1
    return [f for f in files if f["hunks"]]


def annotate(files: list[dict]) -> str:
    """모델이 정확한 줄 번호를 쓰도록 새 파일 기준 줄 번호를 붙인 diff 텍스트."""
    out = []
    for f in files:
        out.append(f"### FILE: {f['file']}")
        for h in f["hunks"]:
            for l in h["lines"]:
                mark = {"add": "+", "del": "-", "ctx": " "}[l["type"]]
                num = l["new"] if l["new"] is not None else "   "
                out.append(f"{num:>5} {mark} {l['text']}")
    return "\n".join(out)


class Comment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    line: int
    severity: Literal["critical", "major", "minor", "nit"]
    category: Literal["bug", "security", "performance", "readability", "test", "design"]
    comment: str
    suggestion: str = Field(description="line 번호의 그 한 줄을 대체할 코드(들여쓰기 포함, 여러 줄 가능). "
                                        "설명 문장·마크다운·코드펜스 금지. 구체적인 코드 수정이 없으면 빈 문자열")


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    verdict: Literal["approve", "request_changes", "comment"]
    comments: List[Comment]


SYSTEM = """당신은 시니어 소프트웨어 엔지니어로서 코드 리뷰를 합니다.
- 추가(+)된 줄 위주로 실제 버그, 보안 취약점, 성능 문제를 우선 지적하세요. 스타일 지적은 최소화.
- line은 반드시 diff 왼쪽에 표시된 새 파일 기준 줄 번호(+ 또는 공백 줄)를 사용하세요.
- suggestion에는 해당 line 한 줄을 그대로 대체할 코드만 넣고(여러 줄 가능, 원래 들여쓰기 유지), 설명은 comment에 쓰세요. 해당 줄 교체로 고칠 수 없으면 빈 문자열.
- 확신이 없는 지적은 하지 마세요. 코멘트와 요약은 한국어로."""


def review_diff(diff: str) -> dict:
    # /review 와 웹훅(_post_review) 양쪽을 여기서 한 번에 막는다
    if len(diff) > MAX_DIFF_CHARS:
        raise HTTPException(413, f"diff가 너무 큽니다 (최대 {MAX_DIFF_CHARS:,}자). 파일 단위로 나눠 요청하세요.")
    files = parse_diff(diff)
    if not files:
        raise HTTPException(400, "유효한 unified diff가 아닙니다.")
    msg = llm.parse([{"role": "user", "content": annotate(files)}], Review, system=SYSTEM, effort="medium")
    review = msg.parsed_output
    valid_lines = {(f["file"], l["new"]) for f in files for h in f["hunks"] for l in h["lines"] if l["new"] is not None}
    kept, dropped = [], []
    for c in review.comments:
        (kept if (c.file, c.line) in valid_lines else dropped).append(c.model_dump())
    return {"summary": review.summary, "verdict": review.verdict, "comments": kept,
            "dropped_invalid_line": dropped, "files": files, "usage": llm.usage_of(msg)}


class ReviewReq(BaseModel):
    diff: str


@router.post("/review")
def review(req: ReviewReq):
    return review_diff(req.diff)


# ---------- GitHub Webhook ----------
@router.post("/github-webhook")
async def github_webhook(request: Request, background: BackgroundTasks):
    """GitHub App/Webhook: pull_request(opened, synchronize) 이벤트 → 리뷰 게시.
    환경변수 GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN 필요."""
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    token = os.getenv("GITHUB_TOKEN")
    if not secret or not token:
        raise HTTPException(503, "GITHUB_WEBHOOK_SECRET / GITHUB_TOKEN 이 설정되지 않았습니다.")
    body = await request.body()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(401, "서명 검증 실패")
    if request.headers.get("X-GitHub-Event") != "pull_request":
        return {"ignored": True}
    payload = json.loads(body)
    if payload.get("action") not in ("opened", "synchronize", "reopened"):
        return {"ignored": True}
    background.add_task(_post_review, payload, token)
    return {"queued": True}


def _suggestion_block(code: str) -> str:
    """GitHub ```suggestion 블록. 모델이 붙인 코드펜스를 벗기고, 내용 속 백틱보다 긴 펜스를 써서 블록이 일찍 닫히지 않게 한다."""
    code = re.sub(r"\A```[\w+-]*[ \t]*\n?", "", code.strip("\n"))
    code = re.sub(r"\n?```[ \t]*\Z", "", code).strip("\n")
    if not code.strip():
        return ""
    fence = "`" * max(3, max(map(len, re.findall(r"`+", code)), default=0) + 1)
    return f"\n\n{fence}suggestion\n{code}\n{fence}"


def _comment_body(c: dict) -> str:
    return f"**[{c['severity']}/{c['category']}]** {c['comment']}" + _suggestion_block(c["suggestion"])


def _post_review(payload: dict, token: str) -> None:
    pr = payload["pull_request"]
    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        resp = httpx.get(pr["url"], headers={**headers, "Accept": "application/vnd.github.v3.diff"}, timeout=30)
        resp.raise_for_status()  # 너무 큰 PR 은 406/422 JSON 을 돌려주므로 diff 로 쓰면 안 된다
        diff = resp.text
        if len(diff) > MAX_DIFF_CHARS:
            log.warning("p06 webhook: %s diff가 너무 큼 (%d자) — 리뷰 생략", pr.get("html_url"), len(diff))
            return
        r = review_diff(diff)
        event = {"approve": "COMMENT", "request_changes": "REQUEST_CHANGES", "comment": "COMMENT"}[r["verdict"]]
        body = {
            "commit_id": pr["head"]["sha"],
            "event": event,
            "body": f"🤖 AI 리뷰 요약\n\n{r['summary']}",
            "comments": [{"path": c["file"], "line": c["line"], "side": "RIGHT", "body": _comment_body(c)}
                         for c in r["comments"]],
        }
        httpx.post(f"{pr['url']}/reviews", headers=headers, json=body, timeout=30).raise_for_status()
    except Exception:
        log.exception("p06 webhook review failed for %s", pr.get("html_url"))
