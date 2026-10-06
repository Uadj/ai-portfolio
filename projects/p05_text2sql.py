"""#05 Text-to-SQL 분석 봇 — Tool use 루프 + 읽기 전용 강제 + 자기 수정 + 차트 스펙 생성."""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter
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


def _readonly_authorizer(action, *_):
    # SELECT/READ/FUNCTION 외의 모든 동작(INSERT, UPDATE, DROP, ATTACH, PRAGMA…) 거부
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, 33}  # 33 = SQLITE_RECURSIVE
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def run_sql(query: str, limit: int = 200) -> dict:
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    con.set_authorizer(_readonly_authorizer)
    try:
        cur = con.execute(query)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit)
        return {"ok": True, "columns": cols, "rows": rows, "truncated": len(rows) == limit}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        con.close()


TOOLS = [
    {
        "name": "run_sql",
        "description": "SQLite 읽기 전용 DB에 SELECT 쿼리를 실행하고 결과(최대 200행)를 돌려준다. 오류가 나면 오류 메시지를 보고 쿼리를 고쳐 다시 실행하라.",
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
                "sql": {"type": "string", "description": "최종 결과를 만드는 SELECT 쿼리"},
                "answer": {"type": "string", "description": "한국어 2~4문장 인사이트"},
                "chart": {"type": "string", "enum": ["bar", "line", "table"]},
                "x": {"type": "string", "description": "x축 컬럼명"},
                "y": {"type": "string", "description": "y축 숫자 컬럼명"},
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
- 먼저 run_sql로 쿼리를 실행해 결과를 확인한 뒤, final_answer 도구로 제출하세요."""


class Ask(BaseModel):
    question: str


@router.get("/schema")
def schema():
    tables = {}
    for t in ["customers", "products", "orders", "order_items"]:
        r = run_sql(f"SELECT * FROM {t} LIMIT 3")
        r["count"] = run_sql(f"SELECT COUNT(*) FROM {t}")["rows"][0][0]
        tables[t] = r
    return {"ddl": SCHEMA, "tables": tables}


@router.post("/ask")
def ask(req: Ask):
    messages = [{"role": "user", "content": req.question}]
    trace, usages = [], []
    final = None
    for _ in range(8):  # 무한 루프 방지
        msg = llm.create(messages, system=SYSTEM, tools=TOOLS, effort="medium", max_tokens=8000)
        usages.append(llm.usage_of(msg))
        messages.append({"role": "assistant", "content": msg.content})
        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "final_answer 도구로 결과를 제출하세요."})
            continue
        results = []
        for tu in tool_uses:
            if tu.name == "final_answer":
                final = tu.input
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "제출 완료"})
            else:
                r = run_sql(tu.input.get("query", ""))
                trace.append({"query": tu.input.get("query"), "ok": r["ok"], "error": r.get("error"),
                              "rows": len(r.get("rows", []))})
                preview = json.dumps({k: (v[:30] if k == "rows" else v) for k, v in r.items()}, ensure_ascii=False, default=str)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": preview, "is_error": not r["ok"]})
        messages.append({"role": "user", "content": results})
        if final:
            break
    if not final:
        return {"error": "최대 반복 횟수 안에 답을 얻지 못했습니다.", "trace": trace, "usage": llm.sum_usage(usages)}
    data = run_sql(final["sql"])
    return {"final": final, "data": data, "trace": trace, "self_corrections": sum(1 for t in trace if not t["ok"]),
            "usage": llm.sum_usage(usages)}


@router.post("/sql")
def raw_sql(req: Ask):
    """읽기 전용 검증용: 사용자가 직접 SQL을 실행 (쓰기 쿼리는 차단됨)."""
    return run_sql(req.question)
