"""#10 멀티 에이전트 — 기획자 → 개발자 → 리뷰어(반복) 파이프라인 vs 단일 에이전트, 블라인드 교차 심사로 품질·비용 비교.

- 비용 상한: 리뷰 라운드는 서버에서 MAX_ROUNDS로 고정한다 (공개 API라 클라이언트 값을 믿지 않는다).
- 심사: 같은 두 코드를 A/B 순서를 바꿔 두 번 심사하고, 두 판정이 일치할 때만 승자를 선언한다 (위치 편향 상쇄).
  단일 실행(n=1) 결과라 승패보다 점수 차와 비용 배수를 함께 보는 것이 맞다.
- 객관 지표: 모델이 쓴 코드는 실행하지 않는다 (방문자가 유도한 코드를 서버에서 돌리면 원격 코드 실행).
  대신 ast로 구문 유효성·assert 수·함수 수만 정적으로 확인한다.
"""
from __future__ import annotations

import ast
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from core import llm, sse

router = APIRouter(prefix="/api/p10")

MAX_ROUNDS = 3          # static/js/demos2.js 입력 max="3"과 맞춘다
MAX_TASK_CHARS = 2000
JUDGE_EFFORT = "low"    # 순서를 바꿔 2회 심사하므로 1회당 effort는 낮춘다 (일일 예산)

PLANNER = "당신은 테크 리드입니다. 요구사항을 분석해 구현 명세(입출력, 엣지 케이스, 테스트 케이스 목록)를 간결한 bullet로 작성하세요. 코드는 쓰지 마세요."
CODER = "당신은 시니어 Python 개발자입니다. 주어진 명세대로 완전한 Python 코드를 작성하세요. 타입 힌트, docstring, 그리고 하단에 assert 기반 테스트를 포함하세요. 코드 블록 하나만 출력하세요."
SINGLE = "당신은 시니어 Python 개발자입니다. 요구사항을 만족하는 완전한 Python 코드를 작성하세요. 타입 힌트, docstring, 하단에 assert 기반 테스트를 포함하세요. 코드 블록 하나만 출력하세요."
REVIEWER = "당신은 깐깐한 코드 리뷰어입니다. 한국어로."
JUDGE = "당신은 공정한 코드 심사위원입니다. 제시 순서나 길이가 아니라 요구사항 충족도로만 판단하세요."

# 구조화 출력의 enum으로 서버가 범위를 강제한다 (Field(ge/le)는 SDK가 설명문으로 옮겨 서버에서 강제되지 않음)
Score10 = Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
SCORE_KEYS = ("correctness", "edge_cases", "readability", "tests")


class ReviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    issues: List[str]


class Score(BaseModel):
    model_config = ConfigDict(extra="forbid")
    correctness: Score10
    edge_cases: Score10
    readability: Score10
    tests: Score10
    rationale: str


class Judge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    solution_a: Score
    solution_b: Score
    winner: Literal["A", "B", "tie"]


def _code(text: str) -> str:
    """첫 코드 블록 본문. 출력 한도로 닫는 ```가 잘린 경우도 처리한다."""
    m = re.search(r"```[\w+-]*[ \t]*\n(.*?)(?:```|\Z)", text, re.S)
    return (m.group(1) if m else text).strip()


def static_checks(src: str) -> dict:
    """코드를 실행하지 않고 ast로만 본다: 구문 유효성, assert 수, 함수 수, 반환 타입 힌트가 있는 함수 수."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return {"syntax_ok": False, "asserts": 0, "functions": 0, "typed_functions": 0}
    nodes = list(ast.walk(tree))
    fns = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return {"syntax_ok": True, "asserts": sum(isinstance(n, ast.Assert) for n in nodes),
            "functions": len(fns), "typed_functions": sum(n.returns is not None for n in fns)}


def _parse_soft(messages: list, model_cls, **kw) -> tuple[BaseModel | None, dict, str]:
    """구조화 출력. 응답이 잘리거나 형식이 깨지면 (None, usage, 사유) — 앞 단계에서 이미 쓴 비용을 버리지 않기 위해.
    거절(422)·인증·예산 오류는 그대로 올린다. (실패한 호출의 비용은 core.llm이 일일 예산에 이미 기록했다)"""
    try:
        msg = llm.parse(messages, model_cls, **kw)
    except HTTPException as e:
        if e.status_code != 502:  # core.llm.parse: 잘림(max_tokens)/스키마 불일치는 502
            raise
        return None, llm.sum_usage([]), str(e.detail)
    return msg.parsed_output, llm.usage_of(msg), ""


def _judge(task: str, a: str, b: str):
    return _parse_soft(
        [{"role": "user", "content": f"요구사항:\n{task}\n\n[Solution A]\n```python\n{a}\n```\n\n[Solution B]\n```python\n{b}\n```\n\n"
                                     "각 항목을 1~10점으로 채점하고 winner는 'A', 'B', 'tie' 중 하나. rationale은 한국어로."}],
        Judge, system=JUDGE, effort=JUDGE_EFFORT, max_tokens=8000)


def _avg(scores: list[Score]) -> dict:
    out: dict = {k: round(sum(getattr(s, k) for s in scores) / len(scores), 1) for k in SCORE_KEYS}
    out["rationale"] = " / ".join(dict.fromkeys(s.rationale.strip() for s in scores if s.rationale.strip()))
    return out


class Req(BaseModel):
    task: str
    max_rounds: int = 2


@router.post("/run")
def run(req: Req):
    task = req.task.strip()
    if not task or len(task) > MAX_TASK_CHARS:
        raise HTTPException(400, f"개발 과제는 1~{MAX_TASK_CHARS}자로 입력해 주세요.")
    rounds = max(0, min(req.max_rounds, MAX_ROUNDS))

    def gen():
        timeline = []

        def step(agent: str, usage: dict, text: str):
            timeline.append({"agent": agent, **usage})
            return sse.event("step", {"agent": agent, "text": text, "usage": usage})

        t0 = time.time()
        # 1) 기획자
        m = llm.create([{"role": "user", "content": task}], system=PLANNER, effort="low", max_tokens=4000)
        spec = llm.text_of(m)
        yield step("planner", llm.usage_of(m), spec)

        # 2) 개발자 ↔ 리뷰어 루프
        convo = [{"role": "user", "content": f"요구사항:\n{task}\n\n명세:\n{spec}"}]
        code = ""
        for rnd in range(rounds + 1):
            m = llm.create(convo, system=CODER, effort="low", max_tokens=8000)
            code = _code(llm.text_of(m))
            cut = "\n\n# ⚠️ 출력 한도에 도달해 코드가 잘렸을 수 있습니다" if m.stop_reason == "max_tokens" else ""
            yield step(f"coder#{rnd + 1}", llm.usage_of(m), code + cut)
            rv, u, err = _parse_soft(
                [{"role": "user", "content": f"명세:\n{spec}\n\n코드:\n```python\n{code}\n```\n\n명세 충족 여부와 버그를 검토하세요. 사소한 스타일은 무시하고, 심각한 문제가 없으면 approved=true."}],
                ReviewOut, system=REVIEWER, effort="low", max_tokens=8000)
            if rv is None:  # 리뷰를 못 읽었으면 재작성(추가 비용)을 요구하지 않고 현재 코드로 진행
                yield step(f"reviewer#{rnd + 1}", u, f"⚠️ 리뷰 응답을 해석하지 못해 현재 코드로 진행합니다\n- {err}")
                break
            yield step(f"reviewer#{rnd + 1}", u, ("✅ 승인" if rv.approved else "❌ 수정 요청") + "\n" + "\n".join(f"- {i}" for i in rv.issues))
            if rv.approved or rnd == rounds:
                break
            convo += [{"role": "assistant", "content": f"```python\n{code}\n```"},
                      {"role": "user", "content": "리뷰 결과를 반영해 전체 코드를 다시 작성하세요:\n" + "\n".join(rv.issues)}]
        multi_time = time.time() - t0
        multi_usage = llm.sum_usage(timeline)

        # 3) 단일 에이전트 베이스라인
        t1 = time.time()
        m = llm.create([{"role": "user", "content": task}], system=SINGLE, effort="low", max_tokens=8000)
        single_code = _code(llm.text_of(m))
        single_usage = llm.usage_of(m)
        single_time = time.time() - t1
        yield sse.event("step", {"agent": "single-agent", "text": single_code, "usage": single_usage})

        # 4) 블라인드 교차 심사: (A=멀티, B=단일)과 (A=단일, B=멀티)를 동시에
        with ThreadPoolExecutor(2) as ex:
            f1, f2 = ex.submit(_judge, task, code, single_code), ex.submit(_judge, task, single_code, code)
            (j1, u1, e1), (j2, u2, e2) = f1.result(), f2.result()
        views = []  # (멀티 점수, 단일 점수, 승자)
        if j1 is not None:
            views.append((j1.solution_a, j1.solution_b, {"A": "multi", "B": "single"}.get(j1.winner, "tie")))
        if j2 is not None:
            views.append((j2.solution_b, j2.solution_a, {"A": "single", "B": "multi"}.get(j2.winner, "tie")))
        if not views:
            raise HTTPException(502, f"심사 응답을 해석하지 못했습니다 ({e1 or e2}). 다시 시도해 주세요.")
        winners = [w for _, _, w in views]
        agree = len(set(winners)) == 1
        yield sse.event("done", {
            "multi": {"code": code, "score": _avg([v[0] for v in views]), "usage": multi_usage,
                      "seconds": round(multi_time, 1), "calls": len(timeline), "static": static_checks(code)},
            "single": {"code": single_code, "score": _avg([v[1] for v in views]), "usage": single_usage,
                       "seconds": round(single_time, 1), "calls": 1, "static": static_checks(single_code)},
            "winner": winners[0] if agree else "tie",  # 순서를 바꾼 두 심사가 엇갈리면 무승부
            "judge_consistent": agree if len(views) == 2 else None,  # None: 한쪽 심사만 성공
            "judge_runs": len(views),
            "judge_usage": {**llm.sum_usage([u1, u2]), "model": u1.get("model") or u2.get("model") or llm.MODEL},
        })

    return sse.response(gen())
