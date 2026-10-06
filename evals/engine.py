"""#11 Eval 엔진 — 웹 API와 CI용 CLI(scripts/run_eval.py)가 공유한다.

채점:
- category / urgency : 정답 라벨과 정확 일치 (결정적 채점)
- reply              : LLM-as-judge 루브릭 1~5점 (공감, 구체적 다음 단계, 과잉 약속 없음, 간결성)

견고성:
- 케이스 하나가 잘림·스키마 불일치·거절로 실패해도 스위트 전체(이미 과금된 다른 케이스)를 버리지 않는다.
  그 케이스는 "error" 행으로 남기고 지표·회귀 비교에서 뺀다.
- 인증·예산·네트워크처럼 모든 케이스가 실패할 오류는 즉시 중단하고, 아직 시작하지 않은 케이스는 취소한다.

통계 메모: 12케이스 LLM 채점은 노이즈가 크다. 긴급도 하나만 뒤집혀도 평균 점수가 0.3/n 움직이므로
게이트 허용 오차는 최소 한 케이스 분량(0.3/n)이고, 케이스 회귀도 순(net) 2건 이상일 때만 실패로 본다.
(동일 프롬프트 A/A 실행으로 노이즈 폭을 재는 보정은 비용 때문에 웹이 아니라 CI에서만 할 일이다.)
"""
from __future__ import annotations

import json
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from core import llm

ROOT = Path(__file__).resolve().parent
DATASET = ROOT.parent / "data" / "evals" / "support_tickets.jsonl"
PROMPTS = ROOT / "prompts"
WORKERS = 4

# 구조화 출력의 enum으로 서버가 범위를 강제한다 (Field(ge/le)는 SDK가 설명문으로 옮겨 서버에서 강제되지 않음)
Score5 = Literal[1, 2, 3, 4, 5]


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["billing", "account", "bug", "feature_request", "how_to"]
    urgency: Literal["low", "medium", "high"]
    reply: str


class Grade(BaseModel):
    model_config = ConfigDict(extra="forbid")
    empathy: Score5
    actionable: Score5
    no_overpromise: Score5
    concise: Score5
    reason: str


JUDGE = """당신은 고객지원 품질 심사관입니다. 고객 문의와 상담원 답변을 보고 각 항목을 1~5점으로 채점하세요.
- empathy: 고객 상황에 공감하는가
- actionable: 고객이 바로 할 수 있는 구체적 다음 단계가 있는가
- no_overpromise: 확인되지 않은 사실(환불 확정, 원인 단정 등)을 약속하지 않는가 (지켰으면 5)
- concise: 불필요하게 길지 않은가
reason은 한국어 한 문장."""


class SuiteFailed(RuntimeError):
    """스위트의 모든 케이스가 실패해 지표를 낼 수 없음."""


def load_cases(limit: int | None = None) -> list[dict]:
    cases = [json.loads(l) for l in DATASET.read_text(encoding="utf-8").splitlines() if l.strip()]
    return cases[:limit] if limit else cases


def prompt_names() -> list[str]:
    return [p.stem for p in sorted(PROMPTS.glob("*.txt"))]


def load_prompt(version: str) -> str:
    # 디스크에 있는 이름만 허용 — glob 결과에는 경로 구분자나 '..'가 없으므로 경로 조작이 막힌다
    names = prompt_names()
    if version not in names:
        raise ValueError(f"알 수 없는 프롬프트 버전: {version!r} (사용 가능: {', '.join(names)})")
    return (PROMPTS / f"{version}.txt").read_text(encoding="utf-8")


def list_prompts() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(PROMPTS.glob("*.txt"))}


def _case_error(e: Exception) -> str | None:
    """케이스 하나만 실패로 처리할 오류면 메시지, 스위트를 멈춰야 할 오류면 None."""
    if isinstance(e, HTTPException):
        # 422: 모델 거절, 502: 구조화 출력 잘림/스키마 불일치 (core.llm). 429·503(예산·키)은 모든 케이스가 실패한다
        return str(e.detail) if e.status_code in (422, 502) else None
    if isinstance(e, ValueError):  # pydantic.ValidationError(잘린 JSON) 포함
        return (str(e).strip().splitlines() or [type(e).__name__])[0]
    return None


def _structured(msg, what: str):
    if getattr(msg, "parsed_output", None) is None:
        raise ValueError(f"{what} 응답을 해석하지 못했습니다 (stop_reason={msg.stop_reason})")
    return msg.parsed_output


def run_case(prompt: str, case: dict) -> dict:
    base = {"id": case["id"], "ticket": case["ticket"],
            "expected": {"category": case["category"], "urgency": case["urgency"]}}
    usages: list[dict] = []
    out = None
    try:
        m = llm.parse([{"role": "user", "content": case["ticket"]}], Output, system=prompt, effort="low", max_tokens=4000)
        usages.append(llm.usage_of(m))
        out = _structured(m, "생성")
        g = llm.parse([{"role": "user", "content": f"[문의]\n{case['ticket']}\n\n[답변]\n{out.reply}"}], Grade,
                      system=JUDGE, effort="low", max_tokens=3000)
        usages.append(llm.usage_of(g))
        grade = _structured(g, "심사")
    except Exception as e:
        err = _case_error(e)
        if err is None:
            raise
        # 실패 행: 화면이 그대로 그릴 수 있는 모양을 유지하고, 지표에서는 빠진다 (usage는 실제로 쓴 만큼)
        return {**base,
                "output": out.model_dump() if out else {"category": "-", "urgency": "-", "reply": ""},
                "grade": {"empathy": 0, "actionable": 0, "no_overpromise": 0, "concise": 0, "reason": f"실행 오류: {err}"},
                "category_ok": bool(out and out.category == case["category"]),
                "urgency_ok": bool(out and out.urgency == case["urgency"]),
                "reply_score": 0.0, "score": 0.0, "pass": False,
                "usage": llm.sum_usage(usages), "error": err[:200]}
    reply_score = (grade.empathy + grade.actionable + grade.no_overpromise + grade.concise) / 20  # 0.2~1
    cat_ok = out.category == case["category"]
    urg_ok = out.urgency == case["urgency"]
    score = 0.4 * cat_ok + 0.3 * urg_ok + 0.3 * reply_score
    return {
        **base,
        "output": out.model_dump(), "grade": grade.model_dump(),
        "category_ok": cat_ok, "urgency_ok": urg_ok, "reply_score": round(reply_score, 3),
        "score": round(score, 3), "pass": cat_ok and urg_ok and reply_score >= 0.7,
        "usage": llm.sum_usage(usages),
    }


def run_suite(version: str, limit: int | None = None, prompt_text: str | None = None) -> dict:
    prompt = prompt_text or load_prompt(version)
    cases = load_cases(limit)
    if not cases:
        raise ValueError("평가할 케이스가 없습니다.")
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(cases))) as ex:
        futures = [ex.submit(run_case, prompt, c) for c in cases]
        done, pending = wait(futures, return_when=FIRST_EXCEPTION)
        fatal = next((f.exception() for f in futures if f in done and f.exception() is not None), None)
        if fatal is not None:
            for f in pending:  # 인증·예산 오류 등: 아직 시작하지 않은 케이스는 돌리지 않는다
                f.cancel()
            raise fatal
        results = [f.result() for f in futures]

    ok = [r for r in results if not r.get("error")]
    if not ok:
        raise SuiteFailed(f"[{version}] 모든 케이스가 실패했습니다: {results[0]['error']}")
    n = len(ok)

    def avg(k: str) -> float:
        return round(sum(r[k] for r in ok) / n, 3)

    return {
        "version": version,
        "n": n,                          # 채점된 케이스 수 (지표의 분모)
        "errors": len(results) - n,      # 실행 오류로 지표에서 뺀 케이스 수
        "category_acc": avg("category_ok"),
        "urgency_acc": avg("urgency_ok"),
        "reply_quality": avg("reply_score"),
        "score": avg("score"),
        "pass_rate": avg("pass"),
        "usage": llm.sum_usage(r["usage"] for r in results),
        "results": results,
    }


def compare(base: dict, cand: dict, tolerance: float = 0.02, max_net_regressions: int = 1) -> dict:
    """양쪽 모두 정상 채점된 케이스만 짝지어 비교한다 (실행 오류는 회귀/개선으로 세지 않는다).

    gate=fail 조건: Δscore < -허용오차(최소 한 케이스 분량 0.3/n) 또는 순 회귀(회귀-개선) > max_net_regressions.
    """
    b = {r["id"]: r for r in base["results"] if not r.get("error")}
    c = {r["id"]: r for r in cand["results"] if not r.get("error")}
    ids = [i for i in c if i in b]
    n = len(ids)
    regressions = [i for i in ids if b[i]["pass"] and not c[i]["pass"]]
    fixes = [i for i in ids if not b[i]["pass"] and c[i]["pass"]]
    if n and n == len(base["results"]) == len(cand["results"]):
        delta = round(cand["score"] - base["score"], 3)  # 오류가 없으면 화면의 지표 차이와 정확히 일치
    else:
        delta = round(sum(c[i]["score"] - b[i]["score"] for i in ids) / n, 3) if n else 0.0
    tol = max(tolerance, 0.3 / n) if n else tolerance
    fail = not n or delta < -tol or len(regressions) - len(fixes) > max_net_regressions
    return {"delta_score": delta, "regressions": regressions, "fixes": fixes, "gate": "fail" if fail else "pass",
            "tolerance": round(tol, 3), "compared": n}
