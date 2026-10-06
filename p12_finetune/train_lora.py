"""2단계: LoRA 파인튜닝 (GPU 권장, 8GB VRAM이면 충분).

pip install torch transformers peft trl datasets accelerate
python p12_finetune/train_lora.py --base Qwen/Qwen2.5-0.5B-Instruct --epochs 3
"""
import argparse
from pathlib import Path

from datasets import load_dataset
from peft import LoraConfig
from trl import SFTConfig, SFTTrainer

HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="Qwen/Qwen2.5-0.5B-Instruct")
ap.add_argument("--epochs", type=int, default=3)
ap.add_argument("--lr", type=float, default=2e-4)
ap.add_argument("--rank", type=int, default=16)
a = ap.parse_args()

ds = load_dataset("json", data_files={"train": str(HERE / "data/train.jsonl"), "test": str(HERE / "data/test.jsonl")})
ds = ds.remove_columns(["key_fact", "doc"])

peft_config = LoraConfig(
    r=a.rank, lora_alpha=a.rank * 2, lora_dropout=0.05, task_type="CAUSAL_LM",
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
)
args = SFTConfig(
    output_dir=str(HERE / "out"), num_train_epochs=a.epochs, learning_rate=a.lr,
    per_device_train_batch_size=8, gradient_accumulation_steps=2, lr_scheduler_type="cosine", warmup_ratio=0.05,
    logging_steps=5, eval_strategy="epoch", save_strategy="epoch", bf16=True,
    assistant_only_loss=True,  # 답변 토큰에만 loss
    report_to="none",
)
trainer = SFTTrainer(model=a.base, args=args, train_dataset=ds["train"], eval_dataset=ds["test"], peft_config=peft_config)
trainer.train()
trainer.save_model(str(HERE / "out" / "final"))
print("saved:", HERE / "out" / "final")
