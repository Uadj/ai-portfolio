"""#01 문서 요약/번역 — 청크 분할 Map-Reduce + 토큰/비용 리포트."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from core import llm

router = APIRouter(prefix="/api/p01")

CHUNK_CHARS = 6000  # 한국어 기준 대략 3~4천 토큰

# 공개 데모 비용 상한: 한 요청이 수천 번의 호출이나 수십만 토큰으로 번지지 않게
MIN_CHUNK, MAX_CHUNK = 1000, 8000
MAX_CHUNKS = 12                 # Map 호출 수 상한 (넘으면 청크 크기를 자동으로 키운다)
MAX_TEXT_CHARS = 120_000        # count_tokens 전에 거르는 1차 상한
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_INPUT_TOKENS = {"summarize": 30_000, "translate": 10_000}  # 번역은 출력이 입력만큼 길어 더 낮게
MAX_OUTPUT_TOKENS = {"summarize": 4000, "translate": 12000}    # 호출당 출력 상한 (thinking 포함)
LANGS = ("한국어", "English", "日本語")
SIZES = {"short": "3문장 이내", "medium": "핵심 bullet 5~7개", "long": "섹션별 상세 요약"}
TEXT_EXTS = (".txt", ".md")


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


def plan_chunks(text: str, chunk_chars: int) -> list[str]:
    """요청한 청크 크기를 [MIN_CHUNK, MAX_CHUNK]로 맞추고, 청크가 MAX_CHUNKS를 넘지 않도록 크기를 키운다."""
    size = min(max(chunk_chars, MIN_CHUNK), MAX_CHUNK)
    size = max(size, -(-len(text) // MAX_CHUNKS))
    chunks = chunk_text(text, size)
    while len(chunks) > MAX_CHUNKS:  # 문단 경계 때문에 더 잘게 나뉜 경우 (size ≥ 전체 길이면 1개가 되므로 반드시 끝난다)
        size = size * 5 // 4 + 1
        chunks = chunk_text(text, size)
    return chunks


def _instruction(mode: str, lang: str, length: str) -> str:
    if mode == "translate":
        return f"다음 텍스트를 {lang}(으)로 자연스럽게 번역하세요. 번역문만 출력하세요."
    return f"다음 문서를 {lang}(으)로 요약하세요. 분량: {SIZES.get(length, SIZES['medium'])}. 사실만, 추측 금지."


def _decode_upload(raw: bytes) -> str:
    """UTF-8(BOM 포함) → CP949(윈도우 메모장 ANSI) 순으로 시도. UTF-16은 BOM이 있을 때만."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise HTTPException(400, "텍스트 인코딩을 인식할 수 없습니다. UTF-8로 저장해 업로드해 주세요.")


def _read_upload(file: UploadFile) -> bytes:
    """상한+1 바이트까지만 읽어 큰 파일을 메모리에 통째로 올리지 않는다."""
    limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"파일은 {limit_mb}MB 이하만 지원합니다.")
    raw = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"파일은 {limit_mb}MB 이하만 지원합니다.")
    return raw


def _count_tokens(content) -> int:
    return llm.client().messages.count_tokens(model=llm.MODEL, messages=[{"role": "user", "content": content}]).input_tokens


def _check_tokens(tokens: int, mode: str) -> None:
    cap = MAX_INPUT_TOKENS[mode]
    if tokens > cap:
        what = "번역" if mode == "translate" else "요약"
        raise HTTPException(413, f"데모에서는 {what}을 약 {cap:,} 토큰까지 처리합니다 (입력 {tokens:,} 토큰). 문서를 나눠서 시도해 주세요.")


def _call(content, instruction: str, max_tokens: int):
    msg = llm.create([{"role": "user", "content": content}], system=instruction, effort="low", max_tokens=max_tokens)
    return llm.text_of(msg), llm.usage_of(msg), msg.stop_reason == "max_tokens"


# 동기 함수: 내부의 블로킹 SDK 호출이 이벤트 루프를 막지 않도록 FastAPI 스레드풀에서 실행된다
@router.post("/run")
def run(
    text: str = Form(""),
    mode: str = Form("summarize"),
    lang: str = Form("한국어"),
    length: str = Form("medium"),
    chunk_chars: int = Form(CHUNK_CHARS),
    file: UploadFile | None = File(None),
):
    if mode not in MAX_INPUT_TOKENS:
        raise HTTPException(400, "mode는 summarize|translate")
    if lang not in LANGS:
        raise HTTPException(400, f"언어는 {' / '.join(LANGS)} 중 하나여야 합니다.")
    instruction = _instruction(mode, lang, length)
    max_out = MAX_OUTPUT_TOKENS[mode]

    if file is not None and file.filename:
        name = file.filename.lower()
        if not name.endswith((".pdf", *TEXT_EXTS)):
            raise HTTPException(400, "지원 형식: .txt / .md / .pdf")
        raw = _read_upload(file)
        # PDF는 Claude의 document 블록으로 직접 전달 (페이지 레이아웃/표까지 이해)
        if name.endswith(".pdf"):
            if b"%PDF-" not in raw[:1024]:
                raise HTTPException(400, "PDF 파일 형식이 아닙니다.")
            content = [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(raw).decode()}},
                {"type": "text", "text": "위 PDF를 처리하세요."},
            ]
            del raw
            tokens = _count_tokens(content)
            _check_tokens(tokens, mode)
            out, u, cut = _call(content, instruction, max_out)
            return {"result": out, "strategy": "pdf-native", "chunks": 1, "input_token_estimate": tokens,
                    "usage": u, "steps": [u], "truncated": cut}
        text = _decode_upload(raw)

    if not text.strip():
        raise HTTPException(400, "텍스트나 파일을 입력하세요.")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"데모에서는 {MAX_TEXT_CHARS:,}자까지 처리합니다 (입력 {len(text):,}자).")

    tokens = _count_tokens(text)
    _check_tokens(tokens, mode)
    chunks = plan_chunks(text, chunk_chars)

    if len(chunks) == 1:
        out, u, cut = _call(text, instruction, max_out)
        return {"result": out, "strategy": "single", "chunks": 1, "input_token_estimate": tokens,
                "usage": u, "steps": [u], "truncated": cut}

    # Map: 청크 병렬 처리. 하나라도 실패하면 아직 시작 안 한 청크는 취소해 과금을 멈춘다
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_call, c, instruction, max_out) for c in chunks]
        try:
            mapped = [f.result() for f in futs]
        except Exception:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
    usages = [u for _, u, _ in mapped]
    truncated = any(cut for _, _, cut in mapped)

    if mode == "translate":
        result = "\n\n".join(o for o, _, _ in mapped)
        return {"result": result, "strategy": "map", "chunks": len(chunks), "input_token_estimate": tokens,
                "usage": llm.sum_usage(usages), "steps": usages, "truncated": truncated}

    # Reduce: 부분 요약을 하나로 통합
    joined = "\n\n".join(f"[파트 {i+1}]\n{o}" for i, (o, _, _) in enumerate(mapped))
    final, u, cut = _call(joined, instruction + " 입력은 긴 문서를 나눠 만든 부분 요약들입니다. 중복을 제거하고 하나로 통합하세요.", max_out)
    usages.append(u)
    return {"result": final, "strategy": "map-reduce", "chunks": len(chunks), "input_token_estimate": tokens,
            "usage": llm.sum_usage(usages), "steps": usages, "truncated": truncated or cut}
