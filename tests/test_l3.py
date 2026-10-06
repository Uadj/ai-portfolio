"""Level 3 (#08~#11) 회귀 테스트 — 실제 API 키 없이 실행.

    python tests/test_l3.py          # pytest 없이
    pytest tests/test_l3.py          # pytest 가 있으면

LLM 경로는 httpx.MockTransport 로 만든 가짜 Claude 응답(스트리밍 포함)으로 검증한다.
#09는 실제 MCP 서버 프로세스(stdio)를 띄워 검증한다 (네트워크 불필요).
L3_NETWORK=1 이면 가짜 키(sk-ant-fake)로 실제 API까지 가서 401 이 나는지도 확인한다.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_server"))

import anthropic  # noqa: E402
import httpx  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import notes_store  # noqa: E402
from core import llm  # noqa: E402
from evals import engine  # noqa: E402
from projects import p08_research_agent, p09_mcp, p10_multi_agent, p11_eval  # noqa: E402

# 다른 영역 파일(server.py 등)이 동시에 수정 중이어도 돌 수 있도록 이 영역 라우터만 올린 앱
app = FastAPI()
for _m in (p08_research_agent, p09_mcp, p10_multi_agent, p11_eval):
    app.include_router(_m.router)


@app.exception_handler(anthropic.APIStatusError)
async def _api_error(_: Request, e: anthropic.APIStatusError):  # server.py 와 동일한 변환
    return JSONResponse({"detail": f"Claude API 오류 ({e.status_code}): {e.message}"}, status_code=502)


client = TestClient(app)


# ---------- 가짜 Claude ----------
class FakeClaude:
    """요청 본문을 받아 응답을 만든다. responder: 응답 dict 목록(차례로) 또는 함수(body → dict | httpx.Response)."""

    def __init__(self, responder):
        self.responder = responder if callable(responder) else self._queue(list(responder))
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    @staticmethod
    def _queue(items):
        return lambda body: items.pop(0)

    def __enter__(self):
        self._saved = llm._client
        llm._client = anthropic.Anthropic(api_key="sk-ant-fake", max_retries=0,
                                          http_client=httpx.Client(transport=httpx.MockTransport(self._handle)))
        return self

    def __exit__(self, *exc):
        llm._client = self._saved

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self._lock:
            self.requests.append(body)
            out = self.responder(body)
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)


def msg(content, stop_reason="end_turn", inp=1000, out=200):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": inp, "output_tokens": out}}


def text(obj):
    return {"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)}


def tool_use(i, name, inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}


def sse_events(res) -> list[tuple[str, dict]]:
    out = []
    for chunk in res.text.split("\n\n"):
        m = re.match(r"event: (\w+)\ndata: (.*)", chunk, re.S)
        if m:
            out.append((m.group(1), json.loads(m.group(2))))
    return out


def system_text(body) -> str:
    s = body.get("system", "")
    return s if isinstance(s, str) else " ".join(b.get("text", "") for b in s)


# ---------- notes_store (#09 저장소) ----------
def _hammer(store: str, n: int, q) -> None:
    sys.path.insert(0, str(ROOT / "mcp_server"))
    import notes_store as ns
    ok = full = 0
    for i in range(n):
        try:
            ns.add_note(f"p{os.getpid()}-{i}", "body", ["t"], store=Path(store))
            ok += 1
        except ValueError:
            full += 1
    q.put((ok, full))


def test_notes_store_concurrent_processes():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "notes.json"
        q = multiprocessing.get_context("spawn").Queue()
        procs = [multiprocessing.get_context("spawn").Process(target=_hammer, args=(str(store), 15, q)) for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        results = [q.get(timeout=5) for _ in procs]
        notes = json.loads(store.read_text(encoding="utf-8"))  # 깨지지 않았다
        ids = [n["id"] for n in notes]
        assert len(ids) == len(set(ids)) == notes_store.MAX_NOTES, ids
        assert sum(ok for ok, _ in results) == notes_store.MAX_NOTES - len(notes_store.SEED)
        assert sum(full for _, full in results) == 60 - (notes_store.MAX_NOTES - len(notes_store.SEED))
        assert not list(Path(d).glob("*.tmp"))


def test_notes_store_validation_and_recovery():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "notes.json"
        for bad in [dict(title=" ", body="b"), dict(title="t" * 101, body="b"), dict(title="t", body="b" * 2001),
                    dict(title="t", body="b", tags=[f"t{i}" for i in range(6)]), dict(title="t", body="b", tags=["x" * 31])]:
            try:
                notes_store.add_note(store=store, **bad)
                raise AssertionError(f"accepted {bad}")
            except ValueError:
                pass
        n = notes_store.add_note("  제목 ", " 본문 ", ["a", "a", " "], store=store)
        assert n["title"] == "제목" and n["tags"] == ["a"] and n["id"] == 4
        store.write_text('[{"id": 1, "title": "x"', encoding="utf-8")  # 반쯤 쓴 파일
        assert [x["id"] for x in notes_store.list_notes(store=store)] == [1, 2, 3]
        assert (Path(d) / "notes.json.corrupt").exists()
        assert notes_store.delete_note(2, store=store) and not notes_store.delete_note(2, store=store)
    for bad in ("../../etc/passwd", "C:/x", "A" * 32, "0" * 31):
        try:
            notes_store.store_for(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert notes_store.store_for(None) == notes_store.STORE
    assert notes_store.store_for("a" * 32).parent == notes_store.SANDBOX_DIR
    assert p09_mcp.SANDBOX_META_KEY == notes_store.SANDBOX_META_KEY


def test_notes_store_sandbox_prune():
    saved = notes_store.SANDBOX_DIR, notes_store.MAX_SANDBOXES
    with tempfile.TemporaryDirectory() as d:
        notes_store.SANDBOX_DIR, notes_store.MAX_SANDBOXES = Path(d) / "s", 5
        try:
            for i in range(9):
                notes_store.list_notes(store=notes_store.store_for(f"{i:032x}"))
                time.sleep(0.02)
            left = sorted(p.stem for p in notes_store.SANDBOX_DIR.glob("*.json"))
            assert left == [f"{i:032x}" for i in range(4, 9)], left
            assert (notes_store.SANDBOX_DIR / ".gitignore").read_text() == "*\n"
        finally:
            notes_store.SANDBOX_DIR, notes_store.MAX_SANDBOXES = saved


# ---------- #09 MCP (실제 stdio 서버) ----------
def _cleanup_sandboxes(*sids):
    for sid in sids:
        if sid:
            for ext in (".json", ".lock"):
                (notes_store.SANDBOX_DIR / f"{sid}{ext}").unlink(missing_ok=True)


def _visit(c, sid, path, body=None):
    """방문자(쿠키)를 바꿔 가며 요청. 하나의 TestClient = 하나의 이벤트 루프 (uvicorn과 동일)."""
    c.cookies.clear()
    headers = {"cookie": f"{p09_mcp.SANDBOX_COOKIE}={sid}"} if sid else {}
    r = c.get(path, headers=headers) if body is None else c.post(path, json=body, headers=headers)
    m = re.search(p09_mcp.SANDBOX_COOKIE + r"=([0-9a-f]{32})", r.headers.get("set-cookie", ""))
    return r, (m.group(1) if m else sid)


def test_p09_hub_reuses_one_server_and_isolates_visitors():
    p09_mcp._listing = None
    sa = sb = None
    with TestClient(app) as c:
        try:
            t = time.monotonic()
            r, _ = _visit(c, None, "/api/p09/tools")
            assert r.status_code == 200, r.text
            first = time.monotonic() - t
            assert [x["name"] for x in r.json()["tools"]] == ["list_notes", "search_notes", "add_note", "delete_note"]
            assert "ctx" not in json.dumps(r.json()["tools"])  # Context 인자는 스키마에 노출되지 않는다
            t = time.monotonic()
            for _ in range(5):
                assert _visit(c, None, "/api/p09/tools")[0].status_code == 200
                r, sa = _visit(c, sa, "/api/p09/call", {"name": "list_notes", "arguments": {}})
                assert r.status_code == 200 and not r.json()["is_error"]
            assert time.monotonic() - t < max(2.0, first), "요청마다 MCP 서버를 새로 띄우고 있다"

            added = _visit(c, sa, "/api/p09/call", {"name": "add_note", "arguments": {"title": "A 전용", "body": "secret-xyz"}})[0].json()
            assert not added["is_error"], added
            search = {"name": "search_notes", "arguments": {"query": "secret-xyz"}}
            assert "secret-xyz" in _visit(c, sa, "/api/p09/call", search)[0].json()["text"]
            r, sb = _visit(c, None, "/api/p09/call", search)
            assert r.json()["text"] == "" and sb and sb != sa  # 다른 방문자에게는 보이지 않는다
            big = _visit(c, sa, "/api/p09/call", {"name": "add_note", "arguments": {"title": "t", "body": "x" * 5000}})[0].json()
            assert big["is_error"] and "2000자" in big["text"]  # ToolError 메시지가 그대로 전달된다
            long_q = _visit(c, sa, "/api/p09/call", {"name": "search_notes", "arguments": {"query": "x" * 201}})[0].json()
            assert long_q["is_error"] and "200자" in long_q["text"]
            assert _visit(c, sa, "/api/p09/call", {"name": "nope", "arguments": {}})[0].json()["is_error"]
            assert _visit(c, sa, "/api/p09/agent", {"prompt": "x" * 1001})[0].status_code == 400
        finally:
            _cleanup_sandboxes(sa, sb)


def test_p09_invalid_cookie_is_replaced():
    sid = None
    with TestClient(app) as c:
        try:
            r, sid = _visit(c, "../../etc/passwd", "/api/p09/call", {"name": "list_notes", "arguments": {}})
            assert r.status_code == 200 and not r.json()["is_error"]
            assert sid != "../../etc/passwd" and re.fullmatch(r"[0-9a-f]{32}", sid)
        finally:
            _cleanup_sandboxes(sid)


def test_p09_agent_loop():
    with TestClient(app) as c:
        c.cookies.set(p09_mcp.SANDBOX_COOKIE, "f" * 32)
        try:
            # 1) 도구 호출 → 결과를 받아 답변. 2) max_tokens로 잘린 tool_use는 실행하지 않는다
            fake = FakeClaude([
                msg([tool_use(1, "add_note", {"title": "에이전트 노트", "body": "b", "tags": ["incident"]})], "tool_use"),
                msg([text("추가했습니다.")]),
            ])
            with fake:
                r = c.post("/api/p09/agent", json={"prompt": "노트 추가해줘"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["answer"] == "추가했습니다." and not body["stopped"] and body["trace"][0]["tool"] == "add_note"
            assert fake.requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"
            assert "지시가 아닙니다" in system_text(fake.requests[0])

            with FakeClaude([msg([text("쓰는 중"), tool_use(2, "add_note", {"title": "잘림", "body": "..."})], "max_tokens")]):
                r = c.post("/api/p09/agent", json={"prompt": "긴 노트"}).json()
            assert r["stopped"] and r["trace"] == [] and "잘려 실행하지 않았습니다" in r["answer"]

            loop = FakeClaude(lambda body: msg([tool_use(9, "list_notes", {})], "tool_use"))
            with loop:
                r = c.post("/api/p09/agent", json={"prompt": "계속"}).json()
            assert r["stopped"] and len(loop.requests) == p09_mcp.MAX_TURNS and "한도" in r["answer"]
        finally:
            _cleanup_sandboxes("f" * 32)


# ---------- #10 멀티 에이전트 ----------
def _p10_responder(review=None, judge=None):
    def respond(body):
        sys_ = system_text(body)
        if "테크 리드" in sys_:
            return msg([text("- spec")])
        if "리뷰어" in sys_:
            return review(body) if review else msg([text({"approved": False, "issues": ["고칠 것"]})])
        if "심사위원" in sys_:
            return judge(body) if judge else msg([text({
                "solution_a": {"correctness": 8, "edge_cases": 7, "readability": 9, "tests": 6, "rationale": "A"},
                "solution_b": {"correctness": 6, "edge_cases": 5, "readability": 7, "tests": 4, "rationale": "B"},
                "winner": "A"})])
        return msg([text("```python\ndef f(x: int) -> int:\n    return x\n\nassert f(1) == 1\n```")])
    return respond


def test_p10_rounds_are_capped():
    fake = FakeClaude(_p10_responder())
    with fake:
        r = client.post("/api/p10/run", json={"task": "f", "max_rounds": 40})
    ev = sse_events(r)
    assert ev[-1][0] == "done", ev[-1]
    # 기획 1 + (개발+리뷰) x (3+1) + 단일 1 + 심사 2
    assert len(fake.requests) == 1 + 2 * (p10_multi_agent.MAX_ROUNDS + 1) + 1 + 2
    with FakeClaude(_p10_responder()) as fake:
        ev = sse_events(client.post("/api/p10/run", json={"task": "f", "max_rounds": -5}))
    assert sum(1 for e, _ in ev if e == "step") == 4 and len(fake.requests) == 6  # 기획·개발·리뷰·단일 + 심사 2
    assert client.post("/api/p10/run", json={"task": " "}).status_code == 400
    assert client.post("/api/p10/run", json={"task": "x" * 2001}).status_code == 400


def test_p10_cross_judging_and_static_checks():
    # 위치 편향 심사위원: 항상 A를 고른다 → 두 판정이 엇갈려 무승부
    with FakeClaude(_p10_responder(review=lambda b: msg([text({"approved": True, "issues": []})]))):
        ev = sse_events(client.post("/api/p10/run", json={"task": "f", "max_rounds": 2}))
    done = dict(ev)["done"]
    assert done["winner"] == "tie" and done["judge_consistent"] is False and done["judge_runs"] == 2
    assert done["multi"]["score"]["correctness"] == 7.0  # (8 + 6) / 2
    assert done["multi"]["static"] == {"syntax_ok": True, "asserts": 1, "functions": 1, "typed_functions": 1}
    assert done["judge_usage"]["model"] and done["judge_usage"]["output_tokens"] == 400

    # 실제로 멀티가 나은 심사위원: 순서와 무관하게 멀티를 고른다 → 일치
    def fair(body):
        content = body["messages"][0]["content"]
        multi_is_a = content.index("[Solution A]") < content.index("MULTI") < content.index("[Solution B]")
        good = {"correctness": 9, "edge_cases": 9, "readability": 9, "tests": 9, "rationale": "좋음"}
        bad = {"correctness": 3, "edge_cases": 3, "readability": 3, "tests": 3, "rationale": "나쁨"}
        return msg([text({"solution_a": good if multi_is_a else bad, "solution_b": bad if multi_is_a else good,
                          "winner": "A" if multi_is_a else "B"})])

    def respond(body):
        sys_ = system_text(body)
        if "주어진 명세대로" in sys_:
            return msg([text("```python\nMULTI = 1\n```")])
        return _p10_responder(review=lambda b: msg([text({"approved": True, "issues": []})]), judge=fair)(body)

    with FakeClaude(respond):
        done = dict(sse_events(client.post("/api/p10/run", json={"task": "f", "max_rounds": 0})))["done"]
    assert done["winner"] == "multi" and done["judge_consistent"] is True and done["multi"]["score"]["tests"] == 9.0

    assert p10_multi_agent.static_checks("def (:") == {"syntax_ok": False, "asserts": 0, "functions": 0, "typed_functions": 0}
    assert p10_multi_agent._code("```python\nx = 1\n") == "x = 1"  # 닫는 ``` 없이 잘린 출력
    schema = json.dumps(anthropic.transform_schema(p10_multi_agent.Judge))
    assert '"enum": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]' in schema and '"enum": ["A", "B", "tie"]' in schema


def test_p10_truncated_review_and_judge():
    cut_review = lambda b: msg([text('{"approved": tr')], "max_tokens")  # noqa: E731
    with FakeClaude(_p10_responder(review=cut_review)) as fake:
        ev = sse_events(client.post("/api/p10/run", json={"task": "f", "max_rounds": 3}))
    steps = [d for e, d in ev if e == "step"]
    assert steps[2]["text"].startswith("⚠️") and ev[-1][0] == "done"
    assert len(fake.requests) == 1 + 2 + 1 + 2  # 리뷰를 못 읽어도 재작성 라운드를 돌지 않는다

    calls = {"n": 0}

    def judge_once_bad(body):
        calls["n"] += 1
        return msg([text("{")], "max_tokens") if calls["n"] == 1 else _p10_responder()(body)

    with FakeClaude(_p10_responder(judge=judge_once_bad, review=lambda b: msg([text({"approved": True, "issues": []})]))):
        done = dict(sse_events(client.post("/api/p10/run", json={"task": "f"})))["done"]
    assert done["judge_runs"] == 1 and done["judge_consistent"] is None

    with FakeClaude(_p10_responder(judge=lambda b: msg([text("{")], "max_tokens"),
                                   review=lambda b: msg([text({"approved": True, "issues": []})]))):
        ev = sse_events(client.post("/api/p10/run", json={"task": "f"}))
    assert ev[-1][0] == "error" and "심사" in ev[-1][1]["detail"] and "NoneType" not in ev[-1][1]["detail"]


# ---------- #11 Eval 엔진 ----------
def _eval_responder(bad_ids=(), judge_bad_ids=(), score=4):
    def respond(body):
        content = body["messages"][0]["content"]
        cid = next((c["id"] for c in engine.load_cases() if c["ticket"] in content), None)
        if "품질 심사관" in system_text(body):
            if cid in judge_bad_ids:
                return msg([text('{"empathy": 4, "act')], "max_tokens")
            return msg([text({"empathy": score, "actionable": score, "no_overpromise": score, "concise": score, "reason": "ok"})])
        if cid in bad_ids:
            return msg([text("")], "max_tokens")
        case = next(c for c in engine.load_cases() if c["id"] == cid)
        return msg([text({"category": case["category"], "urgency": case["urgency"], "reply": "확인해 보겠습니다."})])
    return respond


def test_engine_isolates_case_failures():
    with FakeClaude(_eval_responder(bad_ids=("t02",), judge_bad_ids=("t03",))):
        s = engine.run_suite("v1", 6)
    assert s["n"] == 4 and s["errors"] == 2 and s["score"] == 0.94 and s["category_acc"] == 1.0
    rows = {r["id"]: r for r in s["results"]}
    assert rows["t02"]["error"] and rows["t02"]["output"]["category"] == "-" and rows["t02"]["grade"]["reason"].startswith("실행 오류")
    assert rows["t03"]["error"] and rows["t03"]["output"]["category"] == "feature_request"  # 생성 결과는 보여 준다
    assert rows["t03"]["usage"]["output_tokens"] == 200  # 심사가 실패해도 생성 호출 비용은 남는다

    with FakeClaude(_eval_responder(bad_ids=tuple(f"t{i:02d}" for i in range(1, 13)))):
        try:
            engine.run_suite("v1", 3)
            raise AssertionError("SuiteFailed expected")
        except engine.SuiteFailed:
            pass


def test_engine_fatal_error_stops_suite():
    def auth_fail(body):
        time.sleep(0.05)
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
    fake = FakeClaude(auth_fail)
    with fake:
        try:
            engine.run_suite("v1", 12)
            raise AssertionError("APIStatusError expected")
        except anthropic.AuthenticationError:
            pass
    assert len(fake.requests) < 12  # 남은 케이스는 취소된다


def test_engine_compare_and_prompts():
    def row(i, p, s, err=None):
        r = {"id": i, "pass": p, "score": s}
        return {**r, "error": err} if err else r
    base = {"score": 0.9, "results": [row("a", True, 0.9), row("b", True, 0.9), row("c", False, 0.5, "x")]}
    cand = {"score": 0.4, "results": [row("a", True, 0.9), row("b", False, 0.6), row("c", False, 0.0, "y")]}
    c = engine.compare(base, cand)
    assert c["compared"] == 2 and c["regressions"] == ["b"] and c["delta_score"] == -0.15 and c["tolerance"] == 0.15
    assert c["gate"] == "pass"  # 한 케이스 분량 이내 + 순 회귀 1건은 노이즈 허용
    cand["results"][0] = row("a", False, 0.2)
    assert engine.compare(base, cand)["gate"] == "fail"
    for bad in ("../../requirements", "C:/Windows/win", "nope"):
        try:
            engine.load_prompt(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert engine.load_prompt("v2").startswith("당신은")
    assert '"enum": [1, 2, 3, 4, 5]' in json.dumps(anthropic.transform_schema(engine.Grade))
    t05 = next(c for c in engine.load_cases() if c["id"] == "t05")
    assert t05["urgency"] == "low"


# ---------- #11 API ----------
def test_p11_validation():
    bad = [({"baseline": "../../requirements"}, "기준"), ({"baseline": "nope"}, "기준"), ({"candidate": "v3"}, "후보"),
           ({"candidate_text": "   "}, "비어"), ({"candidate_text": "x" * 4001}, "4000")]
    for body, needle in bad:
        r = client.post("/api/p11/run", json=body)
        assert r.status_code == 400 and needle in r.json()["detail"], (body, r.text)

    seen = []
    saved, saved_reports = engine.run_suite, p11_eval.REPORTS
    tmp = tempfile.TemporaryDirectory()
    p11_eval.REPORTS = Path(tmp.name)  # 실제 실행 기록은 건드리지 않는다
    engine.run_suite = lambda v, limit=None, prompt_text=None: seen.append(limit) or {
        "version": v, "n": limit, "errors": 0, "score": 1.0, "usage": llm.sum_usage([]), "results": []}
    try:
        for lim, want in ((0, 6), (-20, 1), (99, 12), (3, 3)):
            seen.clear()
            r = client.post("/api/p11/run", json={"limit": lim})
            assert r.status_code == 200 and seen == [want, want], (lim, seen, r.text)
        assert len(list(p11_eval.REPORTS.glob("*.json"))) == 4  # 같은 초에 실행돼도 덮어쓰지 않는다
    finally:
        engine.run_suite, p11_eval.REPORTS = saved, saved_reports
        tmp.cleanup()


def test_p11_info_skips_broken_reports():
    saved = p11_eval.REPORTS
    with tempfile.TemporaryDirectory() as d:
        p11_eval.REPORTS = Path(d)
        try:
            (Path(d) / "20260101-000000.json").write_text('{"summary": {"at": "ok"}}', encoding="utf-8")
            (Path(d) / "20260102-000000.json").write_text('{"summ', encoding="utf-8")
            (Path(d) / "20260103-000000.json").write_text('{"x": 1}', encoding="utf-8")
            r = client.get("/api/p11/info")
            assert r.status_code == 200 and r.json()["history"] == [{"at": "ok"}]
            p11_eval._save_report({"summary": {"at": "new"}})
            assert not list(Path(d).glob("*.tmp")) and client.get("/api/p11/info").json()["history"][0] == {"at": "new"}
        finally:
            p11_eval.REPORTS = saved


def test_run_eval_cli():
    sys.path.insert(0, str(ROOT / "scripts"))
    import run_eval
    ns = argparse.Namespace(baseline="v1", candidate=None, baseline_ref="HEAD", limit=None)
    if run_eval._git("rev-parse", "HEAD").returncode == 0:
        base_label, base_text, cand_label, _ = run_eval._resolve(ns)
        assert cand_label == "v2" and base_text  # 바뀐 프롬프트가 없으면 v2 vs (HEAD의 v2 또는 v1)
    with tempfile.TemporaryDirectory() as d:
        cwd = os.getcwd()
        os.chdir(d)
        saved, argv = engine.run_suite, sys.argv
        def no_key(*a, **k):
            raise HTTPException(503, "키 없음")
        engine.run_suite = no_key
        sys.argv = ["run_eval.py"]
        try:
            assert run_eval.main() == 2
            assert "eval 실행 실패" in Path("eval-report.md").read_text(encoding="utf-8")
        finally:
            engine.run_suite, sys.argv = saved, argv
            os.chdir(cwd)


# ---------- #08 리서치 에이전트 ----------
def _stream(blocks, stop_reason, searches=0, fetches=0):
    """content block 목록 → Messages 스트리밍 SSE 응답."""
    ev = [("message_start", {"type": "message_start", "message": {**msg([], None, 500, 1), "content": []}})]
    for i, b in enumerate(blocks):
        start = dict(b)
        delta = None
        if b["type"] == "server_tool_use":
            start["input"], delta = {}, {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}
        elif b["type"] == "text":
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": b["text"]}
        ev.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}))
        if delta:
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                 "usage": {"output_tokens": 300, "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": fetches}}}))
    ev.append(("message_stop", {"type": "message_stop"}))
    body = "".join(f"event: {n}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for n, d in ev)
    return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": "text/event-stream"})


def test_p08_research_flow():
    search = [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "AI 에이전트"}},
              {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                  {"type": "web_search_result", "title": "A", "url": "https://www.a.example/x/", "encrypted_content": "e", "page_age": None}]},
              {"type": "server_tool_use", "id": "srvtoolu_2", "name": "code_execution", "input": {"code": "print(1)"}},
              {"type": "server_tool_use", "id": "srvtoolu_3", "name": "web_fetch", "input": {"url": "https://b.example"}},
              {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_3", "content": {"type": "web_fetch_tool_result_error", "error_code": "url_not_accessible"}}]
    report = [{"type": "text", "text": "## 요약\n본문 [1]\n\n## 출처\n1. A - https://a.example/x.\n2. 가짜 - https://made-up.example/y\n"}]
    fake = FakeClaude([_stream(search, "pause_turn", searches=3, fetches=1), _stream([], "pause_turn", searches=1),
                       _stream(report, "end_turn")])
    with fake:
        r = client.post("/api/p08/research", json={"topic": "AI 에이전트", "max_searches": 5})
    ev = sse_events(r)
    names = [e for e, _ in ev]
    done = ev[-1][1]
    assert names[-1] == "done", ev[-3:]
    assert ("tool_error", {"error": "web_fetch: url_not_accessible"}) in ev
    assert [d["name"] for e, d in ev if e == "tool"] == ["web_search", "web_fetch"]  # code_execution은 '읽기'로 안 보인다
    assert sum(1 for e, d in ev if e == "status" and "필터링" in d["text"]) == 1
    assert done["verified_urls"] == ["https://a.example/x"] and done["unverified_urls"] == ["https://made-up.example/y"]
    assert done["web_searches"] == 4 and done["web_fetches"] == 1 and done["stop_reason"] == "end_turn"
    # max_uses는 요청마다 초기화되므로 남은 횟수를 넘긴다: 5 → 2 → 1(최소)
    uses = [[t["max_uses"] for t in req["tools"] if t["name"] == "web_search"][0] for req in fake.requests]
    assert uses == [5, 2, 1], uses
    assert all(t.get("max_content_tokens") == 6000 for req in fake.requests for t in req["tools"] if t["name"] == "web_fetch")
    assert fake.requests[1]["messages"][-1]["role"] == "assistant" and len(fake.requests) == 3

    loop = FakeClaude(lambda body: _stream([], "pause_turn"))
    with loop:
        ev = sse_events(client.post("/api/p08/research", json={"topic": "x"}))
    assert len(loop.requests) == p08_research_agent.MAX_RESUMES + 1 and ev[-1][1]["stop_reason"] == "pause_turn"
    assert sum(1 for e, _ in ev if e == "status") == p08_research_agent.MAX_RESUMES  # 일어나지 않을 재개는 알리지 않는다

    with FakeClaude([_stream([{"type": "text", "text": "..."}], "refusal")]):
        ev = sse_events(client.post("/api/p08/research", json={"topic": "x"}))
    assert ev[-1][0] == "error" and "거절" in ev[-1][1]["detail"]

    for bad in ("", "x" * 301):
        assert client.post("/api/p08/research", json={"topic": bad}).status_code == 400


def test_p08_echoable_drops_pre_fallback_internals():
    from types import SimpleNamespace as B
    content = [B(type="thinking"), B(type="server_tool_use", id="s1"), B(type="web_search_tool_result", tool_use_id="s1"),
               B(type="server_tool_use", id="s2"), B(type="text"), B(type="fallback"), B(type="thinking"), B(type="text")]
    kept = p08_research_agent._echoable(content)
    assert [b.type for b in kept] == ["server_tool_use", "web_search_tool_result", "text", "fallback", "thinking", "text"]
    assert kept[0].id == "s1"
    plain = [B(type="thinking"), B(type="text")]
    assert p08_research_agent._echoable(plain) is plain


# ---------- 실제 API 왕복 (가짜 키 → 401) ----------
def test_network_401_smoke():
    if os.getenv("L3_NETWORK") != "1":
        return
    llm._client = None
    with TestClient(app) as c:
        r, sid = _visit(c, None, "/api/p09/agent", {"prompt": "노트 보여줘"})
        _cleanup_sandboxes(sid)
        assert r.status_code == 502 and "401" in r.json()["detail"], r.text
    r = client.post("/api/p11/run", json={"limit": 1})
    assert r.status_code == 502 and "401" in r.json()["detail"], r.text
    for path, body in (("/api/p08/research", {"topic": "x"}), ("/api/p10/run", {"task": "f"})):
        ev = sse_events(client.post(path, json=body))
        assert ev[-1][0] == "error" and "401" in ev[-1][1]["detail"], ev


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
