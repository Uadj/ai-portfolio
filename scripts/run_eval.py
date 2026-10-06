"""CI용 Eval 실행기. 후보 프롬프트가 기준보다 나빠지면 exit 1.

사용:  python scripts/run_eval.py --baseline v1 --candidate v2 [--limit 12]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals import engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="v1")
    ap.add_argument("--candidate", default="v2")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="eval-report.json")
    a = ap.parse_args()

    base = engine.run_suite(a.baseline, a.limit)
    cand = engine.run_suite(a.candidate, a.limit)
    cmp = engine.compare(base, cand)

    rows = [("metric", a.baseline, a.candidate)] + [
        (k, base[k], cand[k]) for k in ("category_acc", "urgency_acc", "reply_quality", "score", "pass_rate")
    ]
    md = "| " + " | ".join(rows[0]) + " |\n|---|---|---|\n" + "\n".join(f"| {k} | {b} | {c} |" for k, b, c in rows[1:])
    md += f"\n\n**Δscore {cmp['delta_score']:+}** · regressions {cmp['regressions']} · fixes {cmp['fixes']} · gate **{cmp['gate'].upper()}**\n"
    md += f"\ncost: ${base['usage']['cost_usd'] + cand['usage']['cost_usd']:.4f}\n"
    print(md)
    Path(a.out).write_text(json.dumps({"baseline": base, "candidate": cand, "compare": cmp}, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("eval-report.md").write_text(md, encoding="utf-8")
    return 0 if cmp["gate"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
