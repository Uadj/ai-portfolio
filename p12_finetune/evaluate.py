"""3단계: 베이스 모델 vs LoRA 모델 비교 → results.json (웹 페이지가 이 파일을 읽어 표시).

python p12_finetune/evaluate.py --base Qwen/Qwen2.5-0.5B-Instruct --adapter p12_finetune/out/final
지표: key_fact 포함률(정확 사실 회수율), 평균 답변 길이, 지연시간
"""
import argparse
import json
import time
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="Qwen/Qwen2.5-0.5B-Instruct")
ap.add_argument("--adapter", default=str(HERE / "out" / "final"))
a = ap.parse_args()

tests = [json.loads(l) for l in open(HERE / "data/test.jsonl", encoding="utf-8")]
tok = AutoTokenizer.from_pretrained(a.base)


def run(model, label):
    hits, lens, secs, samples = 0, 0, 0.0, []
    for t in tests:
        msgs = t["messages"][:2]
        ids = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt").to(model.device)
        t0 = time.time()
        out = model.generate(ids, max_new_tokens=128, do_sample=False)
        secs += time.time() - t0
        ans = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True)
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
tuned = PeftModel.from_pretrained(base, a.adapter)
r_tuned = run(tuned, a.base + " + LoRA")
res = {"n_test": len(tests), "base": r_base, "tuned": r_tuned,
       "improvement": round(r_tuned["key_fact_recall"] - r_base["key_fact_recall"], 3)}
(HERE / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in res.items() if k != "samples"}, ensure_ascii=False, indent=2))
