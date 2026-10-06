"""3단계: 베이스 / 베이스 + 원문 문서(RAG 상한) / LoRA 비교 → results.json (웹 페이지가 이 파일을 읽어 표시).

pip install "transformers>=4.56" peft accelerate torch
python p12_finetune/evaluate.py --base Qwen/Qwen2.5-0.5B-Instruct --adapter p12_finetune/out/final
지표: key_fact 포함률(정확 사실 회수율), 평균 답변 길이, 지연시간
- 베이스: 질문만 (가상 회사 규정이라 하한에 가까움)
- 베이스 + 원문 문서: 정답 문서를 컨텍스트로 준 오라클 RAG — 파인튜닝이 실제로 비교돼야 할 상한선
"""
import argparse
import json
import time
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

HERE = Path(__file__).resolve().parent
HANDBOOK = HERE.parent / "data" / "handbook"
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="Qwen/Qwen2.5-0.5B-Instruct")
ap.add_argument("--adapter", default=str(HERE / "out" / "final"))
a = ap.parse_args()

tests = [json.loads(l) for l in open(HERE / "data/test.jsonl", encoding="utf-8")]
tok = AutoTokenizer.from_pretrained(a.base)
docs = {}


def run(model, label, with_doc=False):
    hits, lens, secs, samples = 0, 0, 0.0, []
    for t in tests:
        msgs = t["messages"][:2]
        if with_doc:  # 오라클 컨텍스트: 해당 문항의 원문 문서를 시스템 프롬프트에 첨부
            if t["doc"] not in docs:
                docs[t["doc"]] = (HANDBOOK / t["doc"]).read_text(encoding="utf-8")
            doc = docs[t["doc"]]
            msgs = [{"role": "system", "content": msgs[0]["content"] + "\n\n[참고 문서]\n" + doc}, msgs[1]]
        inputs = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt", return_dict=True).to(model.device)
        t0 = time.time()
        out = model.generate(**inputs, max_new_tokens=128, do_sample=False)
        secs += time.time() - t0
        ans = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        ok = t["key_fact"].replace(" ", "") in ans.replace(" ", "")
        hits += ok
        lens += len(ans)
        if len(samples) < 5:
            samples.append({"q": msgs[1]["content"], "a": ans, "key_fact": t["key_fact"], "ok": ok})
    n = len(tests)
    return {"model": label, "key_fact_recall": round(hits / n, 3), "avg_len": round(lens / n, 1),
            "avg_latency_s": round(secs / n, 3), "samples": samples}


base = AutoModelForCausalLM.from_pretrained(a.base, torch_dtype=torch.bfloat16, device_map="auto")
r_base = run(base, a.base)
r_ctx = run(base, a.base + " + 원문 문서(오라클)", with_doc=True)  # PeftModel이 base를 제자리 수정하므로 반드시 먼저 실행
tuned = PeftModel.from_pretrained(base, a.adapter)
r_tuned = run(tuned, a.base + " + LoRA")
res = {"n_test": len(tests), "base": r_base, "rag": r_ctx, "tuned": r_tuned,
       "improvement": round(r_tuned["key_fact_recall"] - r_base["key_fact_recall"], 3),
       "improvement_vs_rag": round(r_tuned["key_fact_recall"] - r_ctx["key_fact_recall"], 3)}
meta = HERE / "data" / "split_meta.json"
if meta.exists():
    res["split"] = json.loads(meta.read_text(encoding="utf-8"))
(HERE / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
summary = {k: ({x: y for x, y in v.items() if x != "samples"} if k in ("base", "rag", "tuned") else v) for k, v in res.items()}
print(json.dumps(summary, ensure_ascii=False, indent=2))
