"""#02 구조화 데이터 추출 — 구조화 출력(Pydantic) + 의미 검증 + 피드백 재시도."""
from __future__ import annotations

import base64
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict

from core import llm

router = APIRouter(prefix="/api/p02")


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    quantity: float
    unit_price: float
    amount: float


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_name: str
    date: Optional[str]
    items: List[Item]
    subtotal: Optional[float]
    tax: Optional[float]
    total: float
    payment_method: Optional[str]


class Career(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str
    role: str
    start: Optional[str]
    end: Optional[str]
    highlights: List[str]


class Resume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    email: Optional[str]
    phone: Optional[str]
    years_of_experience: Optional[float]
    skills: List[str]
    careers: List[Career]
    education: List[str]


SCHEMAS = {"receipt": Receipt, "resume": Resume}


def validate_semantics(kind: str, obj) -> list[str]:
    """스키마는 구조화 출력이 보장 → 여기서는 '내용상' 오류를 검사한다."""
    errs = []
    if kind == "receipt":
        for it in obj.items:
            if abs(it.quantity * it.unit_price - it.amount) > 1:
                errs.append(f"품목 '{it.name}': 수량×단가({it.quantity * it.unit_price:g}) ≠ 금액({it.amount:g})")
        s = sum(it.amount for it in obj.items)
        base = obj.subtotal if obj.subtotal is not None else obj.total - (obj.tax or 0)
        if obj.items and abs(s - base) > 1:
            errs.append(f"품목 합계({s:g})가 소계({base:g})와 다릅니다")
    else:
        if obj.email and "@" not in obj.email:
            errs.append(f"이메일 형식 오류: {obj.email}")
        if not obj.skills:
            errs.append("skills가 비어 있습니다")
    return errs


@router.post("/extract")
async def extract(kind: str = Form("receipt"), text: str = Form(""), image: UploadFile | None = File(None), max_retries: int = Form(2)):
    if kind not in SCHEMAS:
        raise HTTPException(400, "kind는 receipt|resume")
    content: list = []
    if image is not None and image.filename:
        data = base64.standard_b64encode(await image.read()).decode()
        content.append({"type": "image", "source": {"type": "base64", "media_type": image.content_type or "image/png", "data": data}})
    if text.strip():
        content.append({"type": "text", "text": text})
    if not content:
        raise HTTPException(400, "텍스트나 이미지를 입력하세요.")
    content.append({"type": "text", "text": "위 문서에서 정보를 추출하세요. 문서에 없는 값은 null로 두고 지어내지 마세요."})

    messages = [{"role": "user", "content": content}]
    attempts = []
    for attempt in range(max_retries + 1):
        msg = llm.parse(messages, SCHEMAS[kind], effort="low")
        obj = msg.parsed_output
        errs = validate_semantics(kind, obj)
        attempts.append({"attempt": attempt + 1, "errors": errs, "usage": llm.usage_of(msg)})
        if not errs:
            break
        # 검증 오류를 피드백으로 주고 다시 추출
        messages = messages + [
            {"role": "assistant", "content": obj.model_dump_json()},
            {"role": "user", "content": "검증 오류가 있습니다:\n- " + "\n- ".join(errs) + "\n원문을 다시 확인해 수정된 JSON을 출력하세요. 원문 자체가 맞지 않으면 원문 값을 유지하세요."},
        ]
    return {
        "data": obj.model_dump(),
        "valid": not attempts[-1]["errors"],
        "attempts": attempts,
        "usage": llm.sum_usage(a["usage"] for a in attempts),
        "schema": SCHEMAS[kind].model_json_schema(),
    }
