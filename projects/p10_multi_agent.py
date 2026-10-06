"""#10 멀티 에이전트 — 기획자 → 개발자 → 리뷰어(반복) 파이프라인 vs 단일 에이전트, 블라인드 심사로 품질·비용 비교."""
from __future__ import annotations

import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from core import llm, sse

router = APIRouter(prefix="/api/p10")

PLANNER = "당신은 테크 리드입니다. 요구사항을 분석해 구현 명세(입출력, 엣지 케이스, 테스트 케이스 목록)를 간결한 bullet로 작성하세요. 코드는 쓰지 마세요."
CODER = "당신은 시니어 Python 개발자입니다. 주어진 명세대로 완전한 Python 코드를 작성하세요. 타입 힌트, docstring, 그리고 하단에 assert 기반 테스트를 포함하세요. 코드 블록 하나만 출력하세요."
SINGLE = "당신은 시니어 Python 개발자입니다. 요구사항을 만족하는 완전한 Python 코드를 작성하세요. 타입 힌트, docstring, 하단에 assert 기반 테스트를 포함하세요. 코드 블록 하나만 출력하세요."


class ReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    issues: List[str]


class Score(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correctness: int
    edge_cases: int
    readability: int
    tests: int
    rationale: str


class Judge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    solution_a: Score
    solution_b: Score
    winner: str


def _code(text: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


class Req(BaseModel):
    task: str
    max_rounds: int = 2


@router.post("/run")
def run(req: Req):
    def gen():
        timeline = []

        def step(agent: str, msg, text: str):
            u = llm.usage_of(msg)
            timeline.append({"agent": agent, **u})
            return sse.event("step", {"agent": agent, "text": text, "usage": u})

        t0 = time.time()
        # 1) 기획자
        m = llm.create([{"role": "user", "content": req.task}], system=PLANNER, effort="low", max_tokens=4000)
        spec = llm.text_of(m)
        yield step("planner", m, spec)

        # 2) 개발자 ↔ 리뷰어 루프
        convo = [{"role": "user", "content": f"요구사항:\n{req.task}\n\n명세:\n{spec}"}]
        code = ""
        for rnd in range(req.max_rounds + 1):
            m = llm.create(convo, system=CODER, effort="low", max_tokens=8000)
            code = _code(llm.text_of(m))
            yield step(f"coder#{rnd + 1}", m, code)
            r = llm.parse([{"role": "user", "content": f"명세:\n{spec}\n\n코드:\n```python\n{code}\n```\n\n명세 충족 여부와 버그를 검토하세요. 사소한 스타일은 무시하고, 심각한 문제가 없으면 approved=true."}],
                          ReviewOut, system="당신은 깐깐한 코드 리뷰어입니다. 한국어로.", effort="low", max_tokens=4000)
            rv = r.parsed_output
            yield step(f"reviewer#{rnd + 1}", r, ("✅ 승인" if rv.approved else "❌ 수정 요청") + "\n" + "\n".join(f"- {i}" for i in rv.issues))
            if rv.approved or rnd == req.max_rounds:
                break
            convo += [{"role": "assistant", "content": f"```python\n{code}\n```"},
                      {"role": "user", "content": "리뷰 결과를 반영해 전체 코드를 다시 작성하세요:\n" + "\n".join(rv.issues)}]
        multi_time = time.time() - t0
        multi_usage = llm.sum_usage(timeline)

        # 3) 단일 에이전트 베이스라인
        t1 = time.time()
        m = llm.create([{"role": "user", "content": req.task}], system=SINGLE, effort="low", max_tokens=8000)
        single_code = _code(llm.text_of(m))
        single_usage = llm.usage_of(m)
        single_time = time.time() - t1
        yield sse.event("step", {"agent": "single-agent", "text": single_code, "usage": single_usage})

        # 4) 블라인드 심사 (A/B 순서 무작위화로 위치 편향 제거)
        multi_first = random.random() < 0.5
        a, b = (code, single_code) if multi_first else (single_code, code)
        j = llm.parse([{"role": "user", "content": f"요구사항:\n{req.task}\n\n[Solution A]\n```python\n{a}\n```\n\n[Solution B]\n```python\n{b}\n```\n\n"
                        "각 항목을 1~10점으로 채점하고 winner는 'A', 'B', 'tie' 중 하나. rationale은 한국어로."}],
                      Judge, system="당신은 공정한 코드 심사위원입니다.", effort="medium", max_tokens=4000)
        jo = j.parsed_output
        ms, ss = (jo.solution_a, jo.solution_b) if multi_first else (jo.solution_b, jo.solution_a)
        winner = {"A": "multi" if multi_first else "single", "B": "single" if multi_first else "multi"}.get(jo.winner.strip().upper(), "tie")
        yield sse.event("done", {
            "multi": {"code": code, "score": ms.model_dump(), "usage": multi_usage, "seconds": round(multi_time, 1), "calls": len(timeline)},
            "single": {"code": single_code, "score": ss.model_dump(), "usage": single_usage, "seconds": round(single_time, 1), "calls": 1},
            "winner": winner, "judge_usage": llm.usage_of(j),
        })

    return sse.response(gen())
