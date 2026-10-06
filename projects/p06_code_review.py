"""#06 코드 리뷰 봇 — diff 파싱 → 구조화 리뷰 → 라인 검증 → (선택) GitHub PR 인라인 코멘트."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import List, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from core import llm

router = APIRouter(prefix="/api/p06")


# ---------- diff 파서 ----------
def parse_diff(diff: str) -> list[dict]:
    """unified diff → [{file, hunks:[{header, lines:[{type, old, new, text}]}]}]"""
    files, cur, hunk = [], None, None
    old_no = new_no = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            cur = None
            continue
        if line.startswith("+++ "):
            path = line[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            cur = {"file": path, "hunks": []}
            files.append(cur)
            continue
        if line.startswith("--- ") or cur is None:
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(2))
            hunk = {"header": line, "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
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
    return files


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
    suggestion: str


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    verdict: Literal["approve", "request_changes", "comment"]
    comments: List[Comment]


SYSTEM = """당신은 시니어 소프트웨어 엔지니어로서 코드 리뷰를 합니다.
- 추가(+)된 줄 위주로 실제 버그, 보안 취약점, 성능 문제를 우선 지적하세요. 스타일 지적은 최소화.
- line은 반드시 diff 왼쪽에 표시된 새 파일 기준 줄 번호(+ 또는 공백 줄)를 사용하세요.
- 확신이 없는 지적은 하지 마세요. 코멘트와 요약은 한국어로."""


def review_diff(diff: str) -> dict:
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


def _post_review(payload: dict, token: str) -> None:
    pr = payload["pull_request"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    diff = httpx.get(pr["url"], headers={**headers, "Accept": "application/vnd.github.v3.diff"}, timeout=30).text
    r = review_diff(diff)
    event = {"approve": "COMMENT", "request_changes": "REQUEST_CHANGES", "comment": "COMMENT"}[r["verdict"]]
    body = {
        "commit_id": pr["head"]["sha"],
        "event": event,
        "body": f"🤖 AI 리뷰 요약\n\n{r['summary']}",
        "comments": [{"path": c["file"], "line": c["line"], "side": "RIGHT",
                      "body": f"**[{c['severity']}/{c['category']}]** {c['comment']}\n\n```suggestion\n{c['suggestion']}\n```"
                      if c["suggestion"].strip() else f"**[{c['severity']}/{c['category']}]** {c['comment']}"}
                     for c in r["comments"]],
    }
    httpx.post(f"{pr['url']}/reviews", headers=headers, json=body, timeout=30).raise_for_status()
