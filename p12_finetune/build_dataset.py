"""1단계: 데이터셋 구축 — 문서별 QA 합성 → 환각 필터 → train/test 분할 → chat 포맷 jsonl.

python p12_finetune/build_dataset.py --per-doc 12
"""
import argparse
import json
import random

from synth import DOC_DIR, OUT_DIR, synthesize

SYSTEM = "당신은 루미나랩스 사내 규정 안내 봇입니다. 규정에 근거해 짧게 답하세요."

ap = argparse.ArgumentParser()
ap.add_argument("--per-doc", type=int, default=12)
ap.add_argument("--test-ratio", type=float, default=0.2)
a = ap.parse_args()

rows, cost = [], 0.0
for p in sorted(DOC_DIR.glob("*.md")):
    items, u = synthesize(p.name, a.per_doc)
    cost += u["cost_usd"]
    kept = [i for i in items if i["grounded"]]
    print(f"{p.name}: {len(kept)}/{len(items)} grounded")
    rows += kept

random.Random(0).shuffle(rows)
n_test = int(len(rows) * a.test_ratio)
OUT_DIR.mkdir(exist_ok=True)
for split, part in (("test", rows[:n_test]), ("train", rows[n_test:])):
    with open(OUT_DIR / f"{split}.jsonl", "w", encoding="utf-8") as f:
        for r in part:
            f.write(json.dumps({
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": r["question"]},
                             {"role": "assistant", "content": r["answer"]}],
                "key_fact": r["key_fact"], "doc": r["doc"],
            }, ensure_ascii=False) + "\n")
print(f"train={len(rows) - n_test} test={n_test}  synth cost=${cost:.4f}")
