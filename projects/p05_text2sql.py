"""#05 Text-to-SQL 분석 봇 — Tool use 루프 + 읽기 전용 강제 + 자기 수정 + 차트 스펙 생성."""
from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import llm

router = APIRouter(prefix="/api/p05")
DB_PATH = llm.ROOT / "data" / "shop.db"

SCHEMA = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT, tier TEXT, joined_at DATE);
CREATE TABLE products  (id INTEGER PRIMARY KEY, name TEXT, category TEXT, price INTEGER);
CREATE TABLE orders    (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), ordered_at DATE, status TEXT);
CREATE TABLE order_items (order_id INTEGER REFERENCES orders(id), product_id INTEGER REFERENCES products(id), quantity INTEGER, unit_price INTEGER);
""".strip()


def seed() -> None:
    """결정적(seed 고정) 가상 쇼핑몰 데이터 생성."""
    if DB_PATH.exists():
        return
    rnd = random.Random(42)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    cities = ["서울", "부산", "대구", "인천", "광주", "대전"]
    tiers = ["bronze"] * 5 + ["silver"] * 3 + ["gold"] * 2
    last = "김이박최정강조윤장임"
    first = ["민준", "서연", "도윤", "하은", "지호", "수아", "예준", "지유", "시우", "채원"]
    for i in range(1, 201):
        con.execute("INSERT INTO customers VALUES (?,?,?,?,?)",
                    (i, rnd.choice(last) + rnd.choice(first), rnd.choice(cities), rnd.choice(tiers),
                     str(date(2024, 1, 1) + timedelta(days=rnd.randint(0, 500)))))
    catalog = {
        "전자기기": [("무선 이어폰", 129000), ("스마트워치", 259000), ("보조배터리", 39000), ("블루투스 스피커", 89000)],
        "의류": [("후드티", 59000), ("청바지", 79000), ("패딩", 199000), ("양말 세트", 15000)],
        "식품": [("원두 1kg", 32000), ("견과류 세트", 25000), ("단백질 쉐이크", 45000)],
        "도서": [("파이썬 입문", 28000), ("머신러닝 실전", 36000), ("에세이집", 16000)],
    }
    pid = 0
    products = []
    for cat, items in catalog.items():
        for name, price in items:
            pid += 1
            products.append((pid, price))
            con.execute("INSERT INTO products VALUES (?,?,?,?)", (pid, name, cat, price))
    oid = 0
    start = date(2025, 1, 1)
    for d in range(0, 640):
        day = start + timedelta(days=d)
        season = 1.6 if day.month in (11, 12) else 1.0
        for _ in range(rnd.randint(0, int(6 * season))):
            oid += 1
            status = rnd.choices(["delivered", "shipped", "cancelled", "returned"], [80, 8, 8, 4])[0]
            con.execute("INSERT INTO orders VALUES (?,?,?,?)", (oid, rnd.randint(1, 200), str(day), status))
            for p, price in rnd.sample(products, rnd.randint(1, 3)):
                con.execute("INSERT INTO order_items VALUES (?,?,?,?)", (oid, p, rnd.randint(1, 3), price))
    con.commit()
    con.close()


seed()


# ---------- 공개 데모용 실행 한도 ----------
# /sql 은 LLM을 거치지 않아 일일 예산이 적용되지 않으므로, 쿼리 하나가 서버(0.1 CPU, 512MB)를
# 멈추거나 OOM 시키지 못하도록 시간·크기·메모리를 모두 제한한다.
MAX_SQL_CHARS = 5000
MAX_RESULT_BYTES = 1_000_000      # 결과 전체(행 합계) 대략적인 바이트 예산
MAX_VALUE_BYTES = 200_000         # 문자열/BLOB 값 하나의 최대 길이 (SQLITE_LIMIT_LENGTH)
SQL_TIMEOUT_S = 3.0
SQLITE_HEAP_LIMIT = 64 * 1024 * 1024  # 프로세스 전체 SQLite 힙 상한 (모든 연결 합계)
_DENY_FUNCS = {"randomblob", "zeroblob", "load_extension", "fts3_tokenizer"}
_SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, _SQLITE_RECURSIVE}


def _set_heap_limit() -> None:
    # PRAGMA hard_heap_limit 은 프로세스 전역이며 낮추기만 가능 → 사용자 쿼리로 되돌릴 수 없다.
    # (setlimit 이 없는 Python 3.10 에서도 거대 문자열/정렬로 인한 OOM 을 MemoryError 로 바꿔 준다)
    con = sqlite3.connect(":memory:")
    try:
        con.execute(f"PRAGMA hard_heap_limit={SQLITE_HEAP_LIMIT}")
    finally:
        con.close()


_set_heap_limit()


def _readonly_authorizer(action, arg1=None, arg2=None, *_):
    # SELECT/READ/FUNCTION 외의 모든 동작(INSERT, UPDATE, DROP, ATTACH, PRAGMA…) 거부.
    # SQLITE_FUNCTION 일 때 arg2 = 함수 이름 → 메모리를 크게 잡는 함수는 막는다.
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENY_FUNCS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _cell(v):
    """JSON 으로 보낼 수 없는 값(BLOB, inf)을 짧은 문자열로 바꾼다."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return f"<BLOB {len(b)}B 0x{b[:16].hex()}{'…' if len(b) > 16 else ''}>"
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    return v


def run_sql(query: str, limit: int = 200, timeout: float = SQL_TIMEOUT_S) -> dict:
    """읽기 전용 + 시간/크기 제한 실행. 실패해도 예외 대신 {ok: False, error} 를 돌려준다."""
    if len(query) > MAX_SQL_CHARS:
        return {"ok": False, "error": f"쿼리가 너무 깁니다 (최대 {MAX_SQL_CHARS:,}자)."}
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    con.set_authorizer(_readonly_authorizer)
    deadline = time.monotonic() + timeout
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    if hasattr(con, "setlimit"):  # Python 3.11+ (Render 3.12)
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        con.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
    try:
        cur = con.execute(query)
        cols = [d[0] for d in cur.description or []]
        rows, size = [], 0
        for row in cur:  # fetchmany 대신 한 행씩: 크기 예산을 행 단위로 검사
            size += sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in row)
            if size > MAX_RESULT_BYTES:
                return {"ok": False, "error": f"결과가 너무 큽니다({MAX_RESULT_BYTES // 1_000_000}MB 초과). 집계하거나 컬럼/행을 줄이세요."}
            rows.append([_cell(v) for v in row])
            if len(rows) > limit:  # 한 행 더 읽어 실제로 잘렸는지 판단
                break
        return {"ok": True, "columns": cols, "rows": rows[:limit], "truncated": len(rows) > limit}
    except sqlite3.OperationalError as e:
        if str(e) == "interrupted":
            return {"ok": False, "error": f"쿼리 실행 시간 초과({timeout:g}초). 더 단순한 쿼리로 고치세요."}
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    except MemoryError:
        return {"ok": False, "error": "쿼리가 메모리 한도를 넘었습니다. 집계하거나 결과 크기를 줄이세요."}
    except Exception as e:
        if "too big" in str(e):  # SQLITE_TOOBIG: 값 하나가 MAX_VALUE_BYTES 초과
            return {"ok": False, "error": f"값 하나가 너무 큽니다(최대 {MAX_VALUE_BYTES:,}바이트). {type(e).__name__}: {e}"}
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        con.close()


TOOLS = [
    {
        "name": "run_sql",
        "description": "SQLite 읽기 전용 DB에 SELECT 쿼리를 실행하고 결과(최대 200행, 미리보기 30행)를 돌려준다. 실행 시간은 3초로 제한된다. 오류가 나면 오류 메시지를 보고 쿼리를 고쳐 다시 실행하라.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
    },
    {
        "name": "final_answer",
        "description": "분석이 끝나면 반드시 이 도구로 최종 결과를 제출한다.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "최종 결과를 만드는 SELECT 쿼리 (run_sql로 성공을 확인한 쿼리)"},
                "answer": {"type": "string", "description": "한국어 2~4문장 인사이트"},
                "chart": {"type": "string", "enum": ["bar", "line", "table"]},
                "x": {"type": "string", "description": "x축 컬럼명 (sql 결과 컬럼명/별칭과 정확히 일치)"},
                "y": {"type": "string", "description": "y축 숫자 컬럼명 (sql 결과 컬럼명/별칭과 정확히 일치)"},
            },
            "required": ["sql", "answer", "chart", "x", "y"],
            "additionalProperties": False,
        },
    },
]

SYSTEM = f"""당신은 데이터 분석가입니다. 다음 SQLite 스키마의 가상 쇼핑몰 DB를 분석합니다.
{SCHEMA}
- 매출 = order_items.quantity * order_items.unit_price, 취소(cancelled)/반품(returned) 주문은 매출에서 제외.
- 날짜는 'YYYY-MM-DD' 문자열. 월 단위는 strftime('%Y-%m', ordered_at).
- 먼저 run_sql로 쿼리를 실행해 결과를 확인한 뒤, final_answer 도구로 제출하세요.
- 제출된 sql은 서버가 다시 실행해 검증합니다. 실패하거나 x/y가 결과 컬럼에 없으면 반려되니 고쳐서 다시 제출하세요."""

MAX_QUESTION_CHARS = 500
MAX_TURNS = 8               # 무한 루프 방지
MAX_REQUEST_USD = 0.30      # 요청 하나가 쓸 수 있는 비용 상한 (일일 예산 $1 보호)
_FINAL_KEYS = ("sql", "answer", "chart", "x", "y")


class Ask(BaseModel):
    question: str  # /ask: 자연어 질문, /sql: 원본 SQL (길이 제한은 각 핸들러/run_sql 에서)


@router.get("/schema")
def schema():
    tables = {}
    for t in ["customers", "products", "orders", "order_items"]:
        r = run_sql(f"SELECT * FROM {t} LIMIT 3")
        r["count"] = run_sql(f"SELECT COUNT(*) FROM {t}")["rows"][0][0]
        tables[t] = r
    return {"ddl": SCHEMA, "tables": tables}


def check_final(inp: dict) -> tuple[str | None, dict | None]:
    """final_answer 제출 검증: 필드 → SQL 재실행 → x/y 컬럼 존재·y 숫자 여부. (오류 메시지, 실행 결과)"""
    missing = [k for k in _FINAL_KEYS if not isinstance(inp.get(k), str)]
    if missing:
        return f"필수 필드 누락: {', '.join(missing)}", None
    if inp["chart"] not in ("bar", "line", "table"):
        return f"chart는 bar/line/table 중 하나여야 합니다 (받은 값: {inp['chart']!r})", None
    r = run_sql(inp["sql"])
    if not r["ok"]:
        return f"sql 실행 오류 — {r['error']}", None
    if inp["chart"] != "table":
        cols = r["columns"]
        bad = [f"{a}={inp[a]!r}" for a in ("x", "y") if inp[a] not in cols]
        if bad:
            return f"{', '.join(bad)} 이(가) 결과 컬럼에 없습니다. 결과 컬럼: {cols}", None
        yi = cols.index(inp["y"])
        if r["rows"] and not any(isinstance(row[yi], (int, float)) for row in r["rows"]):
            return f"y 컬럼 {inp['y']!r} 에 숫자 값이 없습니다. 숫자 컬럼을 y로 쓰거나 chart='table'로 제출하세요.", None
    return None, r


def _usage(usages: list[dict]) -> dict:
    return llm.sum_usage(usages)  # 표시용 model 키도 sum_usage가 채운다


@router.post("/ask")
def ask(req: Ask):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "질문을 입력하세요.")
    if len(question) > MAX_QUESTION_CHARS:
        raise HTTPException(413, f"질문이 너무 깁니다 (최대 {MAX_QUESTION_CHARS}자).")
    messages = [{"role": "user", "content": question}]
    trace, usages = [], []
    final = data = None
    for _ in range(MAX_TURNS):
        # 비용 상한은 호출 '전'에 검사: 이미 final_answer 를 받은 턴은 버리지 않는다
        if usages and llm.sum_usage(usages)["cost_usd"] >= MAX_REQUEST_USD:
            return {"error": f"요청당 비용 한도(${MAX_REQUEST_USD:g})에 도달해 중단했습니다. 질문을 더 구체적으로 바꿔 보세요.",
                    "trace": trace, "usage": _usage(usages)}
        # 자동 프롬프트 캐싱: 매 턴 다시 보내는 tools + system + 이전 턴을 캐시 읽기(0.05×)로 과금
        msg = llm.create(messages, system=SYSTEM, tools=TOOLS, effort="medium", max_tokens=8000,
                         cache_control={"type": "ephemeral"})
        usages.append(llm.usage_of(msg))
        messages.append({"role": "assistant", "content": msg.content})
        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "final_answer 도구로 결과를 제출하세요."})
            continue
        # max_tokens 에서 잘린 턴의 tool_use 입력은 부분 객체일 수 있으므로 실행하지 않고 반려한다
        cut = msg.stop_reason == "max_tokens"
        results = []
        for tu in tool_uses:
            inp = tu.input if isinstance(tu.input, dict) else {}
            if cut:
                err, r = "응답이 max_tokens에서 잘렸습니다. 더 짧게 다시 호출하세요.", None
            elif tu.name == "final_answer":
                err, r = check_final(inp)
            elif tu.name == "run_sql":
                r = run_sql(str(inp.get("query", "")))
                err = None if r["ok"] else r["error"]
            else:
                err, r = f"알 수 없는 도구: {tu.name}", None

            if tu.name == "final_answer" and not err:
                final, data = inp, r
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "제출 완료"})
                continue
            query = inp.get("sql") if tu.name == "final_answer" else inp.get("query")
            if tu.name == "final_answer":
                err = f"final_answer 검증 실패: {err}"
            trace.append({"tool": tu.name, "query": query, "ok": err is None, "error": err,
                          "rows": len(r["rows"]) if r and r.get("ok") else 0})
            if r and r.get("ok"):
                content = json.dumps({k: (v[:30] if k == "rows" else v) for k, v in r.items()}, ensure_ascii=False, default=str)
            else:
                content = err + (" 고쳐서 final_answer로 다시 제출하세요." if tu.name == "final_answer" else "")
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": err is not None})
        messages.append({"role": "user", "content": results})
        if final:
            break
    if not final:
        return {"error": "최대 반복 횟수 안에 답을 얻지 못했습니다.", "trace": trace, "usage": _usage(usages)}
    return {"final": final, "data": data, "trace": trace, "self_corrections": sum(1 for t in trace if not t["ok"]),
            "usage": _usage(usages)}


@router.post("/sql")
def raw_sql(req: Ask):
    """읽기 전용 검증용: 사용자가 직접 SQL을 실행 (쓰기 쿼리는 차단됨)."""
    return run_sql(req.question)
