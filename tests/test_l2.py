"""Level 2 (#04~#07) 회귀 테스트 — 실제 API 키 없이 실행.

    python tests/test_l2.py          # pytest 없이
    pytest tests/test_l2.py          # pytest 가 있으면

LLM 경로는 httpx.MockTransport 로 만든 가짜 Claude 응답으로 검증한다.
L2_NETWORK=1 이면 가짜 키(sk-ant-fake)로 실제 API까지 가서 401 이 나는지도 확인한다.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402
import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import llm  # noqa: E402
from projects import p04_rag, p05_text2sql, p06_code_review, p07_multimodal  # noqa: E402

# 다른 영역 파일(server.py 등)이 동시에 수정 중이어도 돌 수 있도록 이 영역 라우터만 올린 앱
app = FastAPI()
for _m in (p04_rag, p05_text2sql, p06_code_review, p07_multimodal):
    app.include_router(_m.router)


@app.exception_handler(anthropic.APIStatusError)
async def _api_error(_: Request, e: anthropic.APIStatusError):  # server.py 와 동일한 변환
    return JSONResponse({"detail": f"Claude API 오류 ({e.status_code}): {e.message}"}, status_code=502)


client = TestClient(app)

MEETING_SAMPLE = """[00:00] 김지현(PM): 다음 달 고객지원 챗봇 출시 건 점검하겠습니다. 현재 RAG 정확도가 어느 정도죠?
[00:12] 박민수(ML): 평가셋 기준 Recall@3이 78%예요. 청킹을 헤딩 기반으로 바꾸면 85%까지 갈 것 같습니다.
[00:30] 김지현(PM): 좋아요. 그건 민수 님이 다음 주 금요일까지 실험해서 공유해 주세요.
[00:41] 이수진(BE): 응답 지연이 p95 기준 6초라서 스트리밍 적용이 필요합니다. 제가 이번 주 안에 SSE로 바꿀게요.
[01:05] 박민수(ML): 프롬프트 인젝션 테스트는 아직 안 했어요. 보안팀이랑 일정 잡아야 할 것 같아요.
[01:15] 김지현(PM): 출시일은 11월 3일로 확정하겠습니다. 인젝션 테스트 일정은 제가 보안팀과 조율할게요.
[01:30] 이수진(BE): 비용 대시보드는 출시 이후로 미루는 게 어떨까요?
[01:38] 김지현(PM): 동의합니다. 출시 후 2주 안에 착수하는 걸로 하죠. 혹시 다국어 지원은 1차 범위에 넣어야 할까요?
[01:50] 박민수(ML): 그건 고객 데이터 보고 다시 판단하면 좋겠습니다."""

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


# ---------- 가짜 Claude ----------
class FakeClaude:
    """미리 넣어 둔 응답을 차례로 돌려주고, 받은 요청 본문을 기록한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __enter__(self):
        self._saved = llm._client
        transport = httpx.MockTransport(self._handle)
        llm._client = anthropic.Anthropic(api_key="sk-ant-fake", max_retries=0, http_client=httpx.Client(transport=transport))
        return self

    def __exit__(self, *exc):
        llm._client = self._saved

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json=self.responses.pop(0))


def msg(content, stop_reason="end_turn", inp=1000, out=200):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": inp, "output_tokens": out}}


def tool_use(i, name, inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}


def text(obj):
    return {"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)}


# ---------- #05 run_sql 한도 ----------
def test_run_sql_limits():
    run = p05_text2sql.run_sql
    t = time.monotonic()
    r = run("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c", timeout=0.5)
    assert not r["ok"] and "시간 초과" in r["error"] and time.monotonic() - t < 3
    assert "not authorized" in run("SELECT length(randomblob(900000000))")["error"]
    assert "zeroblob" in run("SELECT zeroblob(10)")["error"]
    for q in ("DELETE FROM orders", "DROP TABLE customers", "ATTACH DATABASE 'x.db' AS x", "PRAGMA hard_heap_limit=0"):
        assert not run(q)["ok"], q
    assert "너무 큽니다" in run("SELECT printf('%.*c', 150000, 'x') FROM order_items")["error"]
    assert "너무 깁니다" in run("SELECT 1" + " " * 6000)["error"]
    r = run("SELECT strftime('%Y-%m', o.ordered_at) m, SUM(oi.quantity*oi.unit_price) rev FROM orders o "
            "JOIN order_items oi ON oi.order_id=o.id WHERE o.status NOT IN ('cancelled','returned') GROUP BY m")
    assert r["ok"] and len(r["rows"]) == 22 and not r["truncated"]


def test_run_sql_cells_and_truncation():
    run = p05_text2sql.run_sql
    assert run("SELECT * FROM customers LIMIT 200")["truncated"] is False  # 정확히 200행은 잘린 게 아님
    r = run("SELECT * FROM orders")
    assert r["truncated"] is True and len(r["rows"]) == 200
    r = run("SELECT x'ff00', 1e999")
    assert r["rows"] == [["<BLOB 2B 0xff00>", "inf"]]
    res = client.post("/api/p05/sql", json={"question": "SELECT x'ff00' AS b"})
    assert res.status_code == 200 and res.json()["rows"] == [["<BLOB 2B 0xff00>"]]


def test_p05_schema():
    r = client.get("/api/p05/schema").json()
    assert r["tables"]["customers"]["count"] == 200 and len(r["tables"]["orders"]["rows"]) == 3


def test_p05_question_validation():
    assert client.post("/api/p05/ask", json={"question": "  "}).status_code == 400
    assert client.post("/api/p05/ask", json={"question": "가" * 501}).status_code == 413


def test_p05_agent_loop_validates_final_answer():
    good_sql = "SELECT category, SUM(price) AS total FROM products GROUP BY category"
    fake = FakeClaude(
        msg([tool_use(1, "run_sql", {"query": "SELECT bogus FROM products"})], "tool_use"),
        msg([tool_use(2, "run_sql", {"query": good_sql})], "tool_use"),
        # x 가 결과 컬럼에 없음 → 반려되어야 한다
        msg([tool_use(3, "final_answer", {"sql": good_sql, "answer": "a", "chart": "bar", "x": "cat", "y": "total"})], "tool_use"),
        # max_tokens 에서 잘린 제출 → 실행하지 않고 반려
        msg([tool_use(4, "final_answer", {"sql": good_sql, "answer": "a"})], "max_tokens"),
        msg([tool_use(5, "final_answer", {"sql": good_sql, "answer": "b", "chart": "bar", "x": "category", "y": "total"})], "tool_use"),
    )
    with fake:
        r = client.post("/api/p05/ask", json={"question": "카테고리별 가격 합"}).json()
    assert r["final"]["x"] == "category" and r["data"]["columns"] == ["category", "total"] and len(r["data"]["rows"]) == 4
    assert [t["ok"] for t in r["trace"]] == [False, True, False, False]
    assert r["trace"][2]["tool"] == "final_answer" and "결과 컬럼: ['category', 'total']" in r["trace"][2]["error"]
    assert "잘렸습니다" in r["trace"][3]["error"]
    assert r["self_corrections"] == 3 and r["usage"]["model"] == "claude-opus-5-5"
    assert all(req.get("cache_control") == {"type": "ephemeral"} for req in fake.requests)
    # 반려 사유가 is_error tool_result 로 모델에게 전달됐는지
    last_user = fake.requests[3]["messages"][-1]["content"][0]
    assert last_user["is_error"] is True and "final_answer 검증 실패" in last_user["content"]


def test_p05_cost_cap_stops_before_next_call():
    expensive = msg([tool_use(1, "run_sql", {"query": "SELECT 1"})], "tool_use", inp=80_000)  # $0.32
    fake = FakeClaude(expensive)
    with fake:
        r = client.post("/api/p05/ask", json={"question": "아무거나"}).json()
    assert "비용 한도" in r["error"] and len(fake.requests) == 1 and r["trace"][0]["ok"]


# ---------- #04 RAG ----------
def test_p04_eval_flags():
    r = client.get("/api/p04/eval").json()
    assert sum(x["best"] for x in r["rows"]) == 1 and r["best"]["best"] is True
    assert all({"reachable", "ctx_chars@3", "misses"} <= x.keys() for x in r["rows"])
    assert r["best"]["chunker"] == "fixed-200-overlap50" and r["best"]["retriever"] == "hybrid-rrf"
    assert "%p" in r["note"]


def test_p04_search_validation_and_zero_score_filter():
    ok = client.post("/api/p04/search", json={"question": "연차 이월", "k": 3}).json()
    assert 1 <= len(ok["results"]) <= 3 and all(x["score"] > 0 for x in ok["results"])
    assert client.post("/api/p04/search", json={"question": "오늘 날씨 어때?", "retriever": "bm25"}).json()["results"] == []
    assert client.post("/api/p04/search", json={"question": "   "}).status_code == 400
    assert client.post("/api/p04/search", json={"question": "가" * 501}).status_code == 413
    assert client.post("/api/p04/search", json={"question": "연차", "k": -1}).status_code == 400
    assert client.post("/api/p04/search", json={"question": "연차", "k": 10000}).status_code == 400
    assert client.post("/api/p04/search", json={"question": "연차", "chunker": "bogus"}).status_code == 422


def test_p04_ask_abstains_without_llm():
    fake = FakeClaude()
    with fake:
        r = client.post("/api/p04/ask", json={"question": "비트코인 시세 알려줘"}).json()
    assert r["abstained"] is True and r["retrieved"] == [] and r["usage"] is None and not fake.requests


def test_p04_ask_truncated_note():
    fake = FakeClaude(msg([text("남은 연차는 최대 5일")], "max_tokens"))
    with fake:
        r = client.post("/api/p04/ask", json={"question": "남은 연차 내년에 쓸 수 있어?"}).json()
    assert r["truncated"] is True and r["abstained"] is False and "잘렸습니다" in r["answer"][-1]["text"]
    assert fake.requests[0]["messages"][0]["content"][0]["type"] == "document"


# ---------- #06 코드 리뷰 ----------
def _lines(files):
    return [(l["type"], l["new"], l["text"]) for f in files for h in f["hunks"] for l in h["lines"]]


def test_parse_diff_edge_cases():
    parse = p06_code_review.parse_diff
    c1 = "--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n line1\n-line2\n\\ No newline at end of file\n+line2\n+line3\n"
    assert _lines(parse(c1)) == [("ctx", 1, "line1"), ("del", None, "line2"), ("add", 2, "line2"), ("add", 3, "line3")]
    c2 = "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n SELECT 1;\n--- old comment\n SELECT 2;\n"
    assert ("del", None, "-- old comment") in _lines(parse(c2))
    c3 = "--- a/y.c\n+++ b/y.c\n@@ -1,2 +1,4 @@\n int x;\n+++ y;\n+int z;\n int w;\n"
    files = parse(c3)
    assert len(files) == 1 and [n for _, n, _ in _lines(files)] == [1, 2, 3, 4]
    c5 = ("diff --git a/a.py b/a.py\n--- a/a.py\t2024-01-01\n+++ b/a.py\t2024-01-02\n@@ -1 +1 @@\n-a\n+b\n"
          "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
    assert [f["file"] for f in parse(c5)] == ["a.py", "gone.py"]
    assert parse("--- a/f\n+++ b/f\n") == [] and parse("") == []


def test_suggestion_block():
    sb = p06_code_review._suggestion_block
    assert sb("```python\n    return x\n```") == "\n\n```suggestion\n    return x\n```"
    assert sb("a = '```'").startswith("\n\n````suggestion\n")  # 내용 속 백틱보다 긴 펜스
    assert sb("  \n") == ""


def test_p06_review_limits_and_line_filter():
    assert client.post("/api/p06/review", json={"diff": "x" * 60_001}).status_code == 413
    assert client.post("/api/p06/review", json={"diff": "not a diff"}).status_code == 400
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a = 1\n-b = 2\n+b = 3\n"
    review = {"summary": "s", "verdict": "comment", "comments": [
        {"file": "x.py", "line": 2, "severity": "minor", "category": "bug", "comment": "ok", "suggestion": "b = 2"},
        {"file": "x.py", "line": 9, "severity": "minor", "category": "bug", "comment": "phantom", "suggestion": ""}]}
    with FakeClaude(msg([text(review)])):
        r = client.post("/api/p06/review", json={"diff": diff}).json()
    assert [c["line"] for c in r["comments"]] == [2] and len(r["dropped_invalid_line"]) == 1
    # 사고만 하고 잘린 응답 → 500 이 아니라 한국어 502 (core/llm.parse 가 변환)
    with FakeClaude(msg([{"type": "thinking", "thinking": "...", "signature": "sig"}], "max_tokens")):
        res = client.post("/api/p06/review", json={"diff": diff})
    assert res.status_code == 502 and isinstance(res.json()["detail"], str)


# ---------- #07 멀티모달 ----------
def test_count_utterances():
    c = p07_multimodal.count_utterances(MEETING_SAMPLE)
    assert (c["김지현"], c["박민수"], c["이수진"]) == (4, 3, 2)


def test_sniff():
    s = p07_multimodal._sniff
    assert s(PNG_1PX) == "image/png" and s(b"\xff\xd8\xff\xe0") == "image/jpeg" and s(b"GIF89a..") == "image/gif"
    assert s(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp" and s(b"hello") is None


def test_p07_image_validation():
    post = lambda data, q="": client.post("/api/p07/image", files={"file": ("a.png", io.BytesIO(data), "image/png")}, data={"question": q})
    assert post(b"just text, not an image").status_code == 400
    assert post(b"").status_code == 400
    assert post(b"\x89PNG\r\n\x1a\n" + b"0" * p07_multimodal.MAX_IMAGE_BYTES).status_code == 413
    assert post(PNG_1PX, "가" * 501).status_code == 413


def test_p07_image_single_call_with_answer():
    analysis = {"caption": "c", "category": "k", "objects": [{"name": "n", "count": 1}], "text_in_image": [],
                "alt_text": "a", "safety_flags": [], "tags": ["t"], "answer": "답변입니다"}
    fake = FakeClaude(msg([text(analysis)]))
    with fake:
        r = client.post("/api/p07/image", files={"file": ("x.bin", io.BytesIO(PNG_1PX), "application/octet-stream")},
                        data={"question": "  무엇이 보이나요?  "}).json()
    assert len(fake.requests) == 1 and r["answer"] == "답변입니다" and "answer" not in r["analysis"]
    assert r["usage"]["input_tokens"] == 1000
    img = fake.requests[0]["messages"][0]["content"][0]["source"]
    assert img["media_type"] == "image/png"  # content_type 대신 매직 바이트


def test_p07_meeting_counts_and_validation():
    assert client.post("/api/p07/meeting", json={"transcript": "  "}).status_code == 400
    assert client.post("/api/p07/meeting", json={"transcript": "가" * 30_001}).status_code == 413
    minutes = {"title": "t", "summary": "s", "decisions": [], "open_questions": [],
               "action_items": [{"owner": "박민수", "task": "실험", "due": "다음 주 금요일", "priority": "높음"}],
               "speakers": [{"speaker": "김지현(PM)", "utterances": 9, "main_points": []},
                            {"speaker": "박민수", "utterances": 9, "main_points": []},
                            {"speaker": "이수진 (BE)", "utterances": 9, "main_points": []}]}
    fake = FakeClaude(msg([text(minutes)]))
    with fake:
        r = client.post("/api/p07/meeting", json={"transcript": MEETING_SAMPLE}).json()
    assert [s["utterances"] for s in r["minutes"]["speakers"]] == [4, 3, 2] and r["utterances_source"] == "regex"
    schema = fake.requests[0]["output_config"]["format"]["schema"]
    assert "높음" in json.dumps(schema, ensure_ascii=False)


# ---------- 실제 API 왕복 (가짜 키 → 401) ----------
def test_network_401_smoke():
    if os.getenv("L2_NETWORK") != "1":
        return
    llm._client = None
    checks = [
        client.post("/api/p04/ask", json={"question": "남은 연차 내년에 쓸 수 있어?"}),
        client.post("/api/p05/ask", json={"question": "월별 매출 추이"}),
        client.post("/api/p06/review", json={"diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"}),
        client.post("/api/p07/meeting", json={"transcript": MEETING_SAMPLE}),
        client.post("/api/p07/image", files={"file": ("a.png", io.BytesIO(PNG_1PX), "image/png")}, data={"question": "뭐야?"}),
    ]
    for res in checks:
        assert res.status_code == 502 and "401" in res.json()["detail"], res.text


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{'OK' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
