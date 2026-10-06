"""#07 멀티모달 — 이미지 분석(Vision) + 회의록 파이프라인(전사 → 화자별 정리 → 요약 → 액션아이템).

참고: Claude는 오디오 입력을 받지 않으므로, 음성은 STT(Whisper 등)로 전사한 텍스트를 입력으로 받는다.
"""
from __future__ import annotations

import base64
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict

from core import llm

router = APIRouter(prefix="/api/p07")


class DetectedObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    count: int


class ImageAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption: str
    category: str
    objects: List[DetectedObject]
    text_in_image: List[str]
    alt_text: str
    safety_flags: List[str]
    tags: List[str]


@router.post("/image")
async def image(file: UploadFile = File(...), question: str = Form("")):
    media = file.content_type or "image/png"
    if media not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        raise HTTPException(400, "png/jpeg/gif/webp만 지원합니다.")
    data = base64.standard_b64encode(await file.read()).decode()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
        {"type": "text", "text": "이미지를 분석하세요. 모든 텍스트 필드는 한국어로. alt_text는 시각장애인용 스크린리더 대체 텍스트(1~2문장)."
                                 + (f"\n추가 질문: {question}" if question else "")},
    ]
    msg = llm.parse([{"role": "user", "content": content}], ImageAnalysis, effort="low")
    answer = None
    if question:
        qa = llm.create([{"role": "user", "content": content[:1] + [{"type": "text", "text": question}]}], effort="low", max_tokens=2000)
        answer = llm.text_of(qa)
    return {"analysis": msg.parsed_output.model_dump(), "answer": answer, "usage": llm.usage_of(msg)}


class ActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str
    task: str
    due: Optional[str]
    priority: str


class SpeakerStat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker: str
    utterances: int
    main_points: List[str]


class Minutes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    summary: str
    decisions: List[str]
    action_items: List[ActionItem]
    open_questions: List[str]
    speakers: List[SpeakerStat]


class MeetingReq(BaseModel):
    transcript: str


@router.post("/meeting")
def meeting(req: MeetingReq):
    if not req.transcript.strip():
        raise HTTPException(400, "전사 텍스트를 입력하세요.")
    prompt = ("다음은 회의 전사본입니다. 회의록을 작성하세요. 액션 아이템은 담당자와 기한이 명시된 것만 넣고, "
              "기한이 언급되지 않았다면 due는 null. 한국어로 작성.\n\n" + req.transcript)
    msg = llm.parse([{"role": "user", "content": prompt}], Minutes, effort="low")
    return {"minutes": msg.parsed_output.model_dump(), "usage": llm.usage_of(msg)}
