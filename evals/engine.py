"""#11 Eval 엔진 — 웹 API와 CI용 CLI(scripts/run_eval.py)가 공유한다.

채점:
- category / urgency : 정답 라벨과 정확 일치 (결정적 채점)
- reply              : LLM-as-judge 루브릭 1~5점 (공감, 구체적 다음 단계, 과잉 약속 없음, 간결성)
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core import llm

ROOT = Path(__file__).resolve().parent
DATASET = ROOT.parent / "data" / "evals" / "support_tickets.jsonl"
PROMPTS = ROOT / "prompts"


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["billing", "account", "bug", "feature_request", "how_to"]
    urgency: Literal["low", "medium", "high"]
    reply: str


class Grade(BaseModel):
    model_config = ConfigDict(extra="forbid")
    empathy: int
    actionable: int
    no_overpromise: int
    concise: int
    reason: str


JUDGE = """당신은 고객지원 품질 심사관입니다. 고객 문의와 상담원 답변을 보고 각 항목을 1~5점으로 채점하세요.
- empathy: 고객 상황에 공감하는가
- actionable: 고객이 바로 할 수 있는 구체적 다음 단계가 있는가
- no_overpromise: 확인되지 않은 사실(환불 확정, 원인 단정 등)을 약속하지 않는가 (지켰으면 5)
- concise: 불필요하게 길지 않은가
reason은 한국어 한 문장."""


def load_cases(limit: int | None = None) -> list[dict]:
    cases = [json.loads(l) for l in DATASET.read_text(encoding="utf-8").splitlines() if l.strip()]
    return cases[:limit] if limit else cases


def load_prompt(version: str) -> str:
    return (PROMPTS / f"{version}.txt").read_text(encoding="utf-8")


def list_prompts() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(PROMPTS.glob("*.txt"))}


def run_case(prompt: str, case: dict) -> dict:
    m = llm.parse([{"role": "user", "content": case["ticket"]}], Output, system=prompt, effort="low", max_tokens=2000)
    out = m.parsed_output
    g = llm.parse([{"role": "user", "content": f"[문의]\n{case['ticket']}\n\n[답변]\n{out.reply}"}], Grade,
                  system=JUDGE, effort="low", max_tokens=1000)
    grade = g.parsed_output
    reply_score = (grade.empathy + grade.actionable + grade.no_overpromise + grade.concise) / 20  # 0~1
    cat_ok = out.category == case["category"]
    urg_ok = out.urgency == case["urgency"]
    score = 0.4 * cat_ok + 0.3 * urg_ok + 0.3 * reply_score
    return {
        "id": case["id"], "ticket": case["ticket"],
        "expected": {"category": case["category"], "urgency": case["urgency"]},
        "output": out.model_dump(), "grade": grade.model_dump(),
        "category_ok": cat_ok, "urgency_ok": urg_ok, "reply_score": round(reply_score, 3),
        "score": round(score, 3), "pass": cat_ok and urg_ok and reply_score >= 0.7,
        "usage": llm.sum_usage([llm.usage_of(m), llm.usage_of(g)]),
    }


def run_suite(version: str, limit: int | None = None, prompt_text: str | None = None) -> dict:
    prompt = prompt_text or load_prompt(version)
    cases = load_cases(limit)
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda c: run_case(prompt, c), cases))
    n = len(results)
    return {
        "version": version,
        "n": n,
        "category_acc": round(sum(r["category_ok"] for r in results) / n, 3),
        "urgency_acc": round(sum(r["urgency_ok"] for r in results) / n, 3),
        "reply_quality": round(sum(r["reply_score"] for r in results) / n, 3),
        "score": round(sum(r["score"] for r in results) / n, 3),
        "pass_rate": round(sum(r["pass"] for r in results) / n, 3),
        "usage": llm.sum_usage(r["usage"] for r in results),
        "results": results,
    }


def compare(base: dict, cand: dict, tolerance: float = 0.02) -> dict:
    by_id = {r["id"]: r for r in base["results"]}
    regressions = [r["id"] for r in cand["results"] if by_id.get(r["id"], {}).get("pass") and not r["pass"]]
    fixes = [r["id"] for r in cand["results"] if not by_id.get(r["id"], {}).get("pass", True) and r["pass"]]
    delta = round(cand["score"] - base["score"], 3)
    return {"delta_score": delta, "regressions": regressions, "fixes": fixes,
            "gate": "fail" if delta < -tolerance or len(regressions) > len(fixes) else "pass"}
