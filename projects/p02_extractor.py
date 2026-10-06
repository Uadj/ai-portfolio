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

# 공개 데모 비용/안정성 상한
MAX_RETRIES = 3                  # 재시도 상한 (매 시도마다 대화가 길어져 입력 토큰이 누적된다)
MAX_TEXT_CHARS = 20_000          # 영수증·이력서에는 충분한 길이
MAX_IMAGE_BYTES = 3_750_000      # API 이미지 한도 5MB는 base64 기준 → 원본은 약 3.7MB
# 업로드된 바이트의 시그니처로 형식을 판별 (브라우저가 주는 content_type은 믿지 않는다)
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def _image_media_type(raw: bytes) -> str | None:
    for sig, media in _MAGIC:
        if raw.startswith(sig):
            return media
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_image(image: UploadFile) -> tuple[bytes, str]:
    too_big = HTTPException(400, "이미지가 너무 큽니다 (최대 약 3.7MB). 해상도를 줄여 다시 올려 주세요.")
    if image.size is not None and image.size > MAX_IMAGE_BYTES:
        raise too_big
    raw = image.file.read(MAX_IMAGE_BYTES + 1)  # 상한+1 바이트까지만 읽는다
    if len(raw) > MAX_IMAGE_BYTES:
        raise too_big
    media = _image_media_type(raw)
    if media is None:
        raise HTTPException(400, "지원 형식: JPG / PNG / GIF / WebP (HEIC 등은 JPG로 변환해 주세요).")
    return raw, media


def validate_semantics(kind: str, obj) -> list[str]:
    """스키마는 구조화 출력이 보장 → 여기서는 '내용상' 오류를 검사한다."""
    errs = []
    if kind == "receipt":
        for it in obj.items:
            if abs(it.quantity * it.unit_price - it.amount) > 1:
                errs.append(f"품목 '{it.name}': 수량×단가({it.quantity * it.unit_price:g}) ≠ 금액({it.amount:g})")
        s = sum(it.amount for it in obj.items)
        base = obj.subtotal if obj.subtotal is not None else obj.total - (obj.tax or 0)
        # 미국식: 품목합 = 소계(세전). 한국식: 품목가에 부가세 포함 → 품목합 = 합계(공급가액+부가세)
        if obj.items and abs(s - base) > 1 and abs(s - obj.total) > 1:
            errs.append(f"품목 합계({s:g})가 소계({base:g})·총액({obj.total:g}) 어느 쪽과도 맞지 않습니다")
    else:
        if obj.email and "@" not in obj.email:
            errs.append(f"이메일 형식 오류: {obj.email}")
        if not obj.skills:
            errs.append("skills가 비어 있습니다")
    return errs


# 동기 함수: 내부의 블로킹 SDK 호출이 이벤트 루프를 막지 않도록 FastAPI 스레드풀에서 실행된다
@router.post("/extract")
def extract(kind: str = Form("receipt"), text: str = Form(""), image: UploadFile | None = File(None), max_retries: int = Form(2)):
    max_retries = max(0, min(max_retries, MAX_RETRIES))  # 최소 1회 시도 보장
    if kind not in SCHEMAS:
        raise HTTPException(400, "kind는 receipt|resume")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"텍스트는 {MAX_TEXT_CHARS:,}자 이하만 지원합니다 (입력 {len(text):,}자).")
    content: list = []
    if image is not None and image.filename:
        raw, media = _read_image(image)
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.standard_b64encode(raw).decode()}})
    if text.strip():
        content.append({"type": "text", "text": text})
    if not content:
        raise HTTPException(400, "텍스트나 이미지를 입력하세요.")
    content.append({"type": "text", "text": "위 문서에서 정보를 추출하세요. 문서에 없는 값은 null로 두고 지어내지 마세요."})

    messages = [{"role": "user", "content": content}]
    attempts = []
    converged = False
    prev = None
    for attempt in range(max_retries + 1):
        msg = llm.parse(messages, SCHEMAS[kind], effort="low")
        obj = msg.parsed_output
        errs = validate_semantics(kind, obj)
        attempts.append({"attempt": attempt + 1, "errors": errs, "usage": llm.usage_of(msg)})
        if not errs:
            break
        cur = obj.model_dump()
        if cur == prev:  # 피드백 후에도 같은 값 → 원문 자체의 불일치로 보고 더 부르지 않는다
            converged = True
            break
        prev = cur
        # 검증 오류를 피드백으로 주고 다시 추출
        messages = messages + [
            {"role": "assistant", "content": obj.model_dump_json()},
            {"role": "user", "content": "검증 오류가 있습니다:\n- " + "\n- ".join(errs) + "\n원문을 다시 확인해 수정된 JSON을 출력하세요. 원문 자체가 맞지 않으면 원문 값을 유지하세요."},
        ]
    return {
        "data": obj.model_dump(),
        "valid": not attempts[-1]["errors"],
        "converged": converged,  # True면 원문 자체 불일치로 판단해 조기 종료
        "attempts": attempts,
        "usage": llm.sum_usage(a["usage"] for a in attempts),
        "schema": SCHEMAS[kind].model_json_schema(),
    }
