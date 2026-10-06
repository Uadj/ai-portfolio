"""#01 문서 요약/번역 — 청크 분할 Map-Reduce + 토큰/비용 리포트."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from core import llm

router = APIRouter(prefix="/api/p01")

CHUNK_CHARS = 6000  # 한국어 기준 대략 3~4천 토큰


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """문단 경계를 우선으로 size 이하 청크로 자른다."""
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:  # 문단 하나가 너무 길면 강제 분할
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:size])
            p = p[size:]
        if len(cur) + len(p) + 1 > size and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _instruction(mode: str, lang: str, length: str) -> str:
    if mode == "translate":
        return f"다음 텍스트를 {lang}(으)로 자연스럽게 번역하세요. 번역문만 출력하세요."
    sizes = {"short": "3문장 이내", "medium": "핵심 bullet 5~7개", "long": "섹션별 상세 요약"}
    return f"다음 문서를 {lang}(으)로 요약하세요. 분량: {sizes.get(length, sizes['medium'])}. 사실만, 추측 금지."


def _call(content, instruction: str):
    msg = llm.create([{"role": "user", "content": content}], system=instruction, effort="low")
    return llm.text_of(msg), llm.usage_of(msg)


@router.post("/run")
async def run(
    text: str = Form(""),
    mode: str = Form("summarize"),
    lang: str = Form("한국어"),
    length: str = Form("medium"),
    chunk_chars: int = Form(CHUNK_CHARS),
    file: UploadFile | None = File(None),
):
    instruction = _instruction(mode, lang, length)
    usages = []

    # PDF는 Claude의 document 블록으로 직접 전달 (페이지 레이아웃/표까지 이해)
    if file is not None and file.filename and file.filename.lower().endswith(".pdf"):
        data = base64.standard_b64encode(await file.read()).decode()
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
            {"type": "text", "text": "위 PDF를 처리하세요."},
        ]
        tokens = llm.client().messages.count_tokens(model=llm.MODEL, messages=[{"role": "user", "content": content}]).input_tokens
        out, u = _call(content, instruction)
        return {"result": out, "strategy": "pdf-native", "chunks": 1, "input_token_estimate": tokens, "usage": u, "steps": [u]}

    if file is not None and file.filename:
        text = (await file.read()).decode("utf-8", errors="ignore")
    if not text.strip():
        raise HTTPException(400, "텍스트나 파일을 입력하세요.")

    tokens = llm.client().messages.count_tokens(model=llm.MODEL, messages=[{"role": "user", "content": text}]).input_tokens
    chunks = chunk_text(text, max(500, chunk_chars))

    if len(chunks) == 1:
        out, u = _call(text, instruction)
        return {"result": out, "strategy": "single", "chunks": 1, "input_token_estimate": tokens, "usage": u, "steps": [u]}

    # Map: 청크 병렬 처리
    with ThreadPoolExecutor(max_workers=4) as ex:
        mapped = list(ex.map(lambda c: _call(c, instruction), chunks))
    usages = [u for _, u in mapped]

    if mode == "translate":
        result = "\n\n".join(o for o, _ in mapped)
        return {"result": result, "strategy": "map", "chunks": len(chunks), "input_token_estimate": tokens,
                "usage": llm.sum_usage(usages), "steps": usages}

    # Reduce: 부분 요약을 하나로 통합
    joined = "\n\n".join(f"[파트 {i+1}]\n{o}" for i, (o, _) in enumerate(mapped))
    final, u = _call(joined, instruction + " 입력은 긴 문서를 나눠 만든 부분 요약들입니다. 중복을 제거하고 하나로 통합하세요.")
    usages.append(u)
    return {"result": final, "strategy": "map-reduce", "chunks": len(chunks), "input_token_estimate": tokens,
            "usage": llm.sum_usage(usages), "steps": usages}
