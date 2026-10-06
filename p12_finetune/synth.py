"""사내 문서 → instruction 튜닝 데이터셋 합성 (Claude를 '교사 모델'로 사용)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


def synthesize(doc_name: str, n: int = 5) -> tuple[list[dict], dict]:
    text = (DOC_DIR / doc_name).read_text(encoding="utf-8")
    msg = llm.parse([{"role": "user", "content": PROMPT.format(n=n, name=doc_name, text=text)}], QASet, effort="low", max_tokens=6000)
    items = []
    for it in msg.parsed_output.items:
        d = it.model_dump()
        d["doc"] = doc_name
        d["grounded"] = it.key_fact in text  # 환각 필터: key_fact가 원문에 없으면 제외 대상
        items.append(d)
    return items, llm.usage_of(msg)
