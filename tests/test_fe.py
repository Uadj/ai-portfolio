"""프론트엔드(static/) + README 테스트 — 실제 API를 부르지 않는다 (LLM 경로는 가짜 키로 401까지만).

실행:  python tests/test_fe.py      (pytest가 있으면 pytest tests/test_fe.py 도 가능)
Node가 있으면 core.js 헬퍼(md/errMsg/num)를 vm에서 실행해 검사하고, 모든 JS를 node --check로 파싱한다
(배포 대상 브라우저 호환을 위해 ES2020 문법만 쓴다 — Node 14로 파싱되면 통과).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

client = TestClient(server.app, raise_server_exceptions=False)
STATIC = ROOT / "static"
JS = sorted((STATIC / "js").glob("*.js"))
NODE = shutil.which("node")


def _code_only(src: str) -> str:
    """주석을 대충 걷어낸 코드 (문법 검사용 휴리스틱)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in src.splitlines())


# ---------- index.html / 정적 자산 ----------
def test_index_served_with_meta_and_csp():
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'http-equiv="Content-Security-Policy"' in html and "script-src 'self'" in html
    assert 'name="description"' in html and 'property="og:url"' in html
    assert 'rel="icon"' in html
    assert "pretendard@v" in html and "dynamic-subset" in html  # 버전 고정 + 서브셋


def test_assets_share_one_cache_buster_and_exist():
    html = client.get("/").text
    refs = re.findall(r'(?:src|href)="(/static/[^"?]+)\?v=(\d+)"', html)
    assert len(refs) == 7, refs  # style.css + JS 6개
    assert len({v for _, v in refs}) == 1, refs  # 버전이 섞이면 새 core.js + 옛 demos.js 같은 조합이 생긴다
    for path, _ in refs:
        assert client.get(path).status_code == 200, path


def test_no_es2021_syntax():
    for f in JS:
        code = _code_only(f.read_text(encoding="utf-8"))
        for tok in ("||=", "&&=", "??=", ".at(", ".replaceAll(", ".findLast("):
            assert tok not in code, f"{f.name}: {tok}"


def test_node_check_all_js():
    if not NODE:
        return  # Node가 없는 환경에서는 건너뜀
    for f in JS:
        p = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert p.returncode == 0, f"{f.name}: {p.stderr[:300]}"


# ---------- core.js 헬퍼 (Node vm) ----------
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
class AC { constructor() { this.signal = { aborted: false }; } abort() { this.signal.aborted = true; } }
const ctx = { AbortController: AC, console, document: { querySelector() { return null; }, querySelectorAll() { return []; } } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\n;globalThis.__x = { md, errMsg, num, isAbort };", ctx);
const { md, errMsg, num, isAbort } = ctx.__x;
const inp = (value, min, max, def) => ({ value, min, max, defaultValue: def });
const a = inp("20", "0", "3", "2"), b = inp("", "1", "12", "6"), c = inp("-5", "1", "", "6");
console.log(JSON.stringify({
  ol: md("1. **가**\n   - 세부\n2. 나\n\n3. 다"),
  link: md("[출처](https://example.com/a)"),
  xss: md("<img src=x onerror=alert(1)>"),
  e422: errMsg({ detail: [{ loc: ["body", "limit"], msg: "bad int" }, { loc: ["body"], msg: "body missing" }] }, { status: 422 }),
  e502: errMsg(null, { status: 502 }),
  e500: errMsg({}, { status: 500 }),
  estr: errMsg({ detail: "오늘의 데모 사용 한도" }, { status: 429 }),
  clamp: [num(a), a.value, num(b), b.value, num(c)],
  abort: [isAbort({ name: "AbortError" }), isAbort(new Error("x"))],
}));
"""


def _core_helpers() -> dict | None:
    if not NODE:
        return None
    with tempfile.TemporaryDirectory() as d:
        h = Path(d) / "h.js"
        h.write_text(HARNESS, encoding="utf-8")
        p = subprocess.run([NODE, str(h), str(STATIC / "js" / "core.js")], capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr[:500]
    return json.loads(p.stdout)


def test_md_numbered_list_keeps_numbering():
    o = _core_helpers()
    if o is None:
        return
    assert '<ol start="2">' in o["ol"] and '<ol start="3">' in o["ol"], o["ol"]
    assert 'rel="noopener noreferrer nofollow"' in o["link"]
    assert "<img" not in o["xss"] and "&lt;img" in o["xss"]


def test_err_msg_and_num_clamp():
    o = _core_helpers()
    if o is None:
        return
    assert o["e422"] == "입력값 오류: limit: bad int / body missing", o["e422"]
    assert "HTTP 502" in o["e502"] and "{}" not in o["e500"] and "500" in o["e500"]
    assert o["estr"] == "오늘의 데모 사용 한도"  # 앱의 문자열 detail은 그대로
    assert o["clamp"] == [3, 3, 6, 6, 1], o["clamp"]  # 20→3, 빈 값→기본값 6, -5→min 1
    assert o["abort"] == [True, False]


# ---------- 메타·문서 ----------
def test_meta_local_flag_for_ollama_project():
    meta = (STATIC / "js" / "meta.js").read_text(encoding="utf-8")
    line = next(l for l in meta.splitlines() if '"Ollama", "Throughput"' in l)
    assert "local: true" in line and "offline: true" not in line


def test_readme_live_link_and_honest_defaults():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://ai-portfolio-gwtu.onrender.com" in readme
    assert "코드 기본값은 0(무제한)" in readme
    assert "(기본값 1)" not in readme and "(기본값 30)" not in readme


# ---------- 프론트가 기대하는 백엔드 응답 모양 ----------
def test_status_has_budget_fields():
    s = client.get("/api/status").json()
    for k in ("llm_ready", "model", "daily_budget_usd", "spent_today_usd", "rate_per_hour"):
        assert k in s, k


def test_rag_eval_best_is_matchable_by_key():
    r = client.get("/api/p04/eval").json()
    best = r["best"]
    hits = [x for x in r["rows"] if x["chunker"] == best["chunker"] and x["retriever"] == best["retriever"]]
    assert len(hits) == 1  # 프론트는 객체 동일성이 아니라 키로 최고 행을 찾는다


def test_guardrail_benchmark_rule_only_shape():
    r = client.post("/api/p15/benchmark", json={"use_llm_classifier": False}).json()
    assert r["rule"]["detection_rate"] >= 0 and r["llm"] is None
    assert all({"label", "text", "pred_rule", "pred_llm"} <= set(x) for x in r["rows"])


def test_validation_error_is_array_for_err_msg():
    r = client.post("/api/p08/research", json={"topic": "x", "max_searches": "많이"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert r.json()["detail"][0]["loc"][0] == "body"


def test_sse_error_event_reaches_client():
    # 가짜 키 → Claude 401 → SSE error 이벤트 (done 없음). 프론트 sse()는 이를 예외로 바꾼다.
    r = client.post("/api/p03/chat", json={"messages": [{"role": "user", "content": "안녕"}]})
    assert r.status_code == 200 and "event: error" in r.text and "event: done" not in r.text


def _run_all():
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    print("ALL PASSED" if not failed else f"{failed} FAILED")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
