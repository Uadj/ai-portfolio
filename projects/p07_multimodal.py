"""#07 멀티모달 — 이미지 분석(Vision) + 회의록 파이프라인(전사 → 화자별 정리 → 요약 → 액션아이템).

참고: Claude는 오디오 입력을 받지 않으므로, 음성은 STT(Whisper 등)로 전사한 텍스트를 입력으로 받는다.
"""
from __future__ import annotations

import base64
import re
from collections import Counter
from typing import List, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from core import llm

router = APIRouter(prefix="/api/p07")

# Claude API 이미지 한도는 base64 기준 5MB → 원본 3.75MB. 그 이상은 읽지도 않는다 (512MB 인스턴스 보호)
MAX_IMAGE_BYTES = 3_750_000
MAX_QUESTION_CHARS = 500
MAX_TRANSCRIPT_CHARS = 30_000


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
    # 기본값 없음 → 스키마상 필수 + null 허용 (ActionItem.due 와 같은 패턴)
    answer: Optional[str] = Field(description="'추가 질문'이 있을 때만 그 질문에 대한 답(한국어, 마크다운 가능). 질문이 없으면 null")


def _sniff(raw: bytes) -> str | None:
    """매직 바이트로 실제 형식을 판별한다 (클라이언트가 보낸 content_type 은 믿지 않음)."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


# 동기 def: FastAPI 가 스레드풀에서 실행 → 블로킹 Claude 호출이 이벤트 루프(다른 방문자 요청·SSE)를 멈추지 않는다
@router.post("/image")
def image(file: UploadFile = File(...), question: str = Form("")):
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise HTTPException(413, f"추가 질문은 {MAX_QUESTION_CHARS}자 이하로 입력하세요.")
    limit_msg = f"이미지는 {MAX_IMAGE_BYTES / 1e6:.2f}MB 이하만 지원합니다. 해상도를 줄여 다시 올려 주세요."
    if file.size and file.size > MAX_IMAGE_BYTES:
        raise HTTPException(413, limit_msg)
    raw = file.file.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(413, limit_msg)
    media = _sniff(raw)
    if not raw or media is None:
        raise HTTPException(400, "png/jpeg/gif/webp 이미지만 지원합니다.")
    data = base64.standard_b64encode(raw).decode()
    del raw  # 수십 초 걸리는 Claude 호출 동안 원본 사본까지 들고 있지 않는다 (512MB 인스턴스)
    prompt = ("이미지를 분석하세요. 모든 텍스트 필드는 한국어로. alt_text는 시각장애인용 스크린리더 대체 텍스트(1~2문장). "
              "추가 질문이 있으면 answer 필드에 답하고, 없으면 answer는 null."
              + (f"\n추가 질문: {question}" if question else ""))
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
        {"type": "text", "text": prompt},
    ]
    # 한 번의 호출로 분석 + 질문 답변 (이미지를 두 번 보내지 않고, 비용도 한 번만 기록)
    msg = llm.parse([{"role": "user", "content": content}], ImageAnalysis, effort="low")
    analysis = msg.parsed_output.model_dump()
    answer = analysis.pop("answer", None)  # analysis 키는 예전과 동일하게 유지
    return {"analysis": analysis, "answer": answer if question else None, "usage": llm.usage_of(msg)}


class ActionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str
    task: str
    due: Optional[str] = Field(description="전사본에 나온 기한 표현 그대로(예: '다음 주 금요일'). 언급이 없으면 null")
    priority: Literal["높음", "보통", "낮음"]


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


# '[00:12] 박민수(ML): …', '00:12 박민수: …', '박민수: …' 형태의 화자 라벨
_SPK = re.compile(r"^(?:\[?\d{1,2}(?::\d{2}){1,2}\]?\s*)?([^:：\n\[\]]{1,30})[:：]", re.M)


def _norm_speaker(name: str) -> str:
    """'김지현(PM)', '김지현 (PM)', '김지현' → '김지현'"""
    return re.sub(r"\s*[(（][^)）]*[)）]", "", name).replace(" ", "").strip()


def count_utterances(transcript: str) -> Counter:
    return Counter(_norm_speaker(m.group(1)) for m in _SPK.finditer(transcript))


@router.post("/meeting")
def meeting(req: MeetingReq):
    transcript = req.transcript.strip()
    if not transcript:
        raise HTTPException(400, "전사 텍스트를 입력하세요.")
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise HTTPException(413, f"전사본이 너무 깁니다 (최대 {MAX_TRANSCRIPT_CHARS:,}자).")
    prompt = ("다음은 회의 전사본입니다. 회의록을 작성하세요. "
              "액션 아이템에는 담당자가 정해진 할 일(지명되었거나 '제가 하겠다'처럼 자청한 것)을 모두 넣으세요. "
              "due는 전사본 표현 그대로(예: \"다음 주 금요일\") 적고 날짜로 환산하거나 추측하지 마세요. 기한 언급이 없으면 null. "
              "priority는 높음/보통/낮음 중 하나. 한국어로 작성.\n\n" + transcript)
    msg = llm.parse([{"role": "user", "content": prompt}], Minutes, effort="low")
    minutes = msg.parsed_output
    # 발언 수는 모델 추정 대신 화자 라벨을 직접 센다. 모델이 나열한 화자만 덮어써서 '참고:' 같은 오탐 라벨은 표에 넣지 않는다
    counts = count_utterances(transcript)
    measured = 0
    for s in minutes.speakers:
        key = _norm_speaker(s.speaker)
        if key and key in counts:
            s.utterances = counts[key]
            measured += 1
    src = "regex" if minutes.speakers and measured == len(minutes.speakers) else ("mixed" if measured else "llm")
    return {"minutes": minutes.model_dump(), "utterances_source": src, "usage": llm.usage_of(msg)}
