"""사내 문서 → instruction 튜닝 데이터셋 합성 (Claude를 '교사 모델'로 사용)."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List

from pydantic import BaseModel, ConfigDict

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:  # CLI(build_dataset.py) 실행용 — 중복 삽입 방지
    sys.path.insert(0, _ROOT)
from core import llm  # noqa: E402

DOC_DIR = llm.ROOT / "data" / "handbook"
OUT_DIR = Path(__file__).resolve().parent / "data"


class QA(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str
    answer: str
    key_fact: str  # 정답 판정용: 답변에 반드시 들어가야 하는 핵심 사실 (원문 그대로의 짧은 구절)
    style: str  # casual / formal / indirect


class QASet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[QA]


PROMPT = """아래 사내 규정 문서로 직원 질의응답 학습 데이터를 {n}개 만드세요.
- 질문은 실제 직원이 메신저에 쓰듯 다양한 말투(casual/formal/indirect)로, 문서 표현을 그대로 베끼지 말 것.
- answer는 문서에 근거한 1~2문장 한국어 답변.
- key_fact는 문서 원문에 그대로 등장하는 5~20자 구절.
- 문서에 없는 내용은 만들지 말 것.

[문서: {name}]
{text}"""


def _norm(s: str) -> str:
    """공백 무시 비교용 (evaluate.py의 key_fact 판정과 같은 기준)."""
    return re.sub(r"\s+", "", s)


def _doc_path(doc_name: str) -> Path:
    """핸드북 폴더 바로 아래의 .md 파일만 허용 (경로 조작 방지 — 호출자 검증과 별개의 2차 방어)."""
    p = (DOC_DIR / doc_name).resolve()
    if p.parent != DOC_DIR.resolve() or p.suffix != ".md" or not p.is_file():
        raise ValueError(f"unknown doc: {doc_name}")
    return p


def synthesize(doc_name: str, n: int = 5) -> tuple[list[dict], dict]:
    text = _doc_path(doc_name).read_text(encoding="utf-8")
    # Opus는 항상 사고(thinking)하고 그 토큰도 max_tokens에 포함되므로 개수에 비례해 여유를 둔다.
    # 잘림(max_tokens)·스키마 불일치는 core.llm.parse가 HTTPException(502)로 바꿔 올린다 (비용은 예산에 기록됨)
    msg = llm.parse([{"role": "user", "content": PROMPT.format(n=n, name=doc_name, text=text)}], QASet,
                    effort="low", max_tokens=max(6000, 2000 + 600 * n))
    usage = llm.usage_of(msg)
    norm_text = _norm(text)
    items = []
    for it in msg.parsed_output.items:
        d = it.model_dump()
        d["doc"] = doc_name
        kf = _norm(it.key_fact)
        # 환각 필터: 공백 무시 비교, 너무 짧은 구절('', '5' 등)은 아무 문서에나 들어 있으므로 근거로 인정하지 않음
        d["grounded"] = len(kf) >= 3 and kf in norm_text
        items.append(d)
    return items, usage
