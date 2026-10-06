"""CI용 Eval 실행기. 후보 프롬프트가 기준보다 나빠지면 exit 1, eval 실행 자체가 실패하면 exit 2.

사용:
  python scripts/run_eval.py                                  # 로컬: v1 vs v2
  python scripts/run_eval.py --baseline v1 --candidate v2 --limit 6
  python scripts/run_eval.py --baseline-ref origin/main       # CI: PR의 프롬프트 vs base 브랜치의 같은 프롬프트

--baseline-ref REF 모드:
- 후보: --candidate가 있으면 그것, 없으면 PR에서 바뀐 프롬프트 중 가장 높은 버전(없으면 전체 중 최신 버전).
- 기준: git의 REF 시점 같은 파일. REF에 없거나(새 버전 추가) 내용이 같으면(엔진·데이터만 바뀐 PR)
  바로 이전 버전 프롬프트를 기준으로 쓴다.
12케이스 LLM 채점은 노이즈가 크므로 게이트는 한 케이스 분량의 허용 오차를 둔다 (evals/engine.py compare 참고).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals import engine  # noqa: E402

REPORT_MD = Path("eval-report.md")


def _version_key(name: str) -> int:
    m = re.fullmatch(r"v(\d+)", name)
    return int(m.group(1)) if m else -1


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def _changed_prompts(ref: str, names: list[str]) -> list[str]:
    r = _git("diff", "--name-only", f"{ref}...HEAD", "--", "evals/prompts/")
    if r.returncode != 0:
        raise RuntimeError(f"git diff 실패 ({ref}): {r.stderr.strip()}")
    stems = {Path(p).stem for p in r.stdout.split() if p.endswith(".txt")}
    return [n for n in names if n in stems]


def _resolve(a) -> tuple[str, str, str, str]:
    """(기준 라벨, 기준 프롬프트 텍스트, 후보 라벨, 후보 이름)."""
    names = sorted(engine.prompt_names(), key=_version_key)
    if not a.baseline_ref:
        return a.baseline, engine.load_prompt(a.baseline), a.candidate or "v2", a.candidate or "v2"
    cand = a.candidate or max(_changed_prompts(a.baseline_ref, names) or names, key=_version_key)
    cand_text = engine.load_prompt(cand)
    shown = _git("show", f"{a.baseline_ref}:evals/prompts/{cand}.txt")
    if shown.returncode == 0 and shown.stdout != cand_text:
        return f"{a.baseline_ref}:{cand}", shown.stdout, cand, cand
    # 새 파일이거나 프롬프트가 그대로면 직전 버전과 비교 (같은 텍스트끼리 비교하면 노이즈만 재게 된다)
    older = [n for n in names if _version_key(n) < _version_key(cand)]
    prev = older[-1] if older else cand
    why = "새 프롬프트" if shown.returncode != 0 else "프롬프트 변경 없음"
    print(f"[info] {why} → 기준을 직전 버전 {prev}로 사용", file=sys.stderr)
    return prev, engine.load_prompt(prev), cand, cand


def _report(base_label: str, cand_label: str, base: dict, cand: dict, cmp: dict) -> str:
    rows = [(k, base[k], cand[k]) for k in ("category_acc", "urgency_acc", "reply_quality", "score", "pass_rate", "n", "errors")]
    md = f"### Prompt eval: `{cand_label}` vs `{base_label}`\n\n"
    md += f"| metric | {base_label} (기준) | {cand_label} (후보) |\n|---|---|---|\n" + "\n".join(f"| {k} | {b} | {c} |" for k, b, c in rows)
    md += (f"\n\n**Δscore {cmp['delta_score']:+}** (허용 오차 ±{cmp['tolerance']}, 비교 {cmp['compared']}건) · "
           f"regressions {cmp['regressions']} · fixes {cmp['fixes']} · gate **{cmp['gate'].upper()}**\n")
    if base["errors"] or cand["errors"]:
        md += f"\n⚠️ 실행 오류로 제외된 케이스: 기준 {base['errors']}건 · 후보 {cand['errors']}건\n"
    md += f"\ncost: ${base['usage']['cost_usd'] + cand['usage']['cost_usd']:.4f}\n"
    return md


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="v1")
    ap.add_argument("--candidate", default=None, help="기본: v2 (--baseline-ref 모드에서는 바뀐 최신 프롬프트)")
    ap.add_argument("--baseline-ref", default=None, help="예: origin/main — 기준을 이 git ref의 같은 프롬프트로")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="eval-report.json")
    a = ap.parse_args()

    try:
        base_label, base_text, cand_label, cand_name = _resolve(a)
        base = engine.run_suite(base_label, a.limit, prompt_text=base_text)
        cand = engine.run_suite(cand_name, a.limit)
        cmp = engine.compare(base, cand)
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 PR 코멘트에 이유를 남긴다
        detail = getattr(e, "detail", None) or str(e)
        md = f"❌ eval 실행 실패: {type(e).__name__}: {detail}\n"
        print(md, file=sys.stderr)
        REPORT_MD.write_text(md, encoding="utf-8")
        return 2

    md = _report(base_label, cand_label, base, cand, cmp)
    print(md)
    Path(a.out).write_text(json.dumps({"baseline": base, "candidate": cand, "compare": cmp}, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_MD.write_text(md, encoding="utf-8")
    return 0 if cmp["gate"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
