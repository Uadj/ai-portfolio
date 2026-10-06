"""1단계: 데이터셋 구축 — 문서별 QA 합성 → 환각 필터 → 중복 제거 → 사실 단위 train/test 분할 → chat 포맷 jsonl.

python p12_finetune/build_dataset.py --per-doc 12

분할 정책: 같은 사실(문서 + key_fact)을 묻는 질문을 한 그룹으로 묶고, 2개 이상인 그룹에서만 1개씩 test로 뺀다.
→ test는 '학습한 사실을 처음 보는 말투로 묻는' 문항이고, 정규화한 질문이 train/test에 동시에 들어가지 않는다.
"""
import argparse
import json
import random
import re
from collections import defaultdict

from synth import DOC_DIR, OUT_DIR, synthesize

SYSTEM = "당신은 루미나랩스 사내 규정 안내 봇입니다. 규정에 근거해 짧게 답하세요."
SPLIT_POLICY = "test = 학습한 사실, 처음 보는 말투"


def norm_key(s: str) -> str:
    """공백·문장부호를 지운 비교 키 (한글은 \\w에 포함되어 유지됨)."""
    return re.sub(r"\W", "", s).lower()


def split_rows(rows: list[dict], test_ratio: float, seed: int = 0) -> tuple[list[dict], list[dict], dict]:
    """질문 중복 제거 → (문서, key_fact) 그룹 단위로 test 추출. (train, test, meta)를 반환."""
    seen, uniq = set(), []
    for r in rows:
        k = norm_key(r["question"])
        if k and k not in seen:
            seen.add(k)
            uniq.append(r)

    groups = defaultdict(list)
    for r in uniq:
        groups[(r["doc"], norm_key(r["key_fact"]))].append(r)
    rng = random.Random(seed)
    keys = sorted(groups)
    rng.shuffle(keys)

    n_test = int(len(uniq) * test_ratio)
    train, test = [], []
    for k in keys:
        g = groups[k]
        rng.shuffle(g)
        if len(g) >= 2 and len(test) < n_test:  # 형제 문항이 train에 남는 사실만 test로 (처음 보는 '사실'은 측정 대상이 아님)
            test.append(g[0])
            train += g[1:]
        else:
            train += g
    rng.shuffle(train)

    train_q = {norm_key(r["question"]) for r in train}
    overlap = sum(norm_key(r["question"]) in train_q for r in test)
    assert overlap == 0, "train/test 질문 중복"
    meta = {"split_policy": SPLIT_POLICY, "n_groups": len(groups), "n_dupes_removed": len(rows) - len(uniq),
            "exact_question_overlap": overlap, "n_train": len(train), "n_test": len(test)}
    return train, test, meta


def write_jsonl(path, part):
    with open(path, "w", encoding="utf-8") as f:
        for r in part:
            f.write(json.dumps({
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": r["question"]},
                             {"role": "assistant", "content": r["answer"]}],
                "key_fact": r["key_fact"], "doc": r["doc"],
            }, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-doc", type=int, default=12)
    ap.add_argument("--test-ratio", type=float, default=0.2)
    a = ap.parse_args()

    rows, cost, failed = [], 0.0, []
    for p in sorted(DOC_DIR.glob("*.md")):
        try:
            items, u = synthesize(p.name, a.per_doc)
        except Exception as e:  # 문서 하나가 실패해도 이미 만든 행은 버리지 않는다
            print(f"{p.name}: 실패 {e}")
            failed.append(p.name)
            continue
        cost += u["cost_usd"]
        kept = [i for i in items if i["grounded"]]
        print(f"{p.name}: {len(kept)}/{len(items)} grounded")
        rows += kept

    train, test, meta = split_rows(rows, a.test_ratio)
    meta["failed_docs"] = failed
    OUT_DIR.mkdir(exist_ok=True)
    write_jsonl(OUT_DIR / "train.jsonl", train)
    write_jsonl(OUT_DIR / "test.jsonl", test)
    (OUT_DIR / "split_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"train={len(train)} test={len(test)} groups={meta['n_groups']} dupes_removed={meta['n_dupes_removed']}  synth cost=${cost:.4f}")


if __name__ == "__main__":
    main()
