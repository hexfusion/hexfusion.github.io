"""
QLoRA fine-tuning script for Go distributed systems training data.

Loads a quantized base model, applies LoRA adapters, and trains on
JSONL instruction/output pairs extracted from public Go repos.

Usage:
    python scripts/train_qlora.py \
        --model Qwen/Qwen2.5-7B-Instruct-AWQ \
        --data data/training_data.jsonl \
        --output output/go-distributed-lora \
        --epochs 3
"""

import argparse
import json
import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
)
from trl import SFTTrainer


def load_jsonl(path):
    """Load training pairs from JSONL file."""
    records = []
    with open(path) as f:
        for line in f:
            rec = json.loads(line)
            records.append(rec)
    print(f"loaded {len(records)} training pairs from {path}")
    return records


def format_prompt(rec):
    """Format a training record as a chat-style prompt."""
    return (
        f"### Instruction:\n{rec['instruction']}\n\n"
        f"### Response:\n{rec['output']}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct-AWQ")
    parser.add_argument("--data", default="data/training_data.jsonl")
    parser.add_argument("--output", default="output/go-distributed-lora")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--max-len", type=int, default=1024)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    args = parser.parse_args()

    # Load data
    records = load_jsonl(args.data)
    dataset = Dataset.from_list([{"text": format_prompt(r)} for r in records])

    # Split 95/5
    split = dataset.train_test_split(test_size=0.05, seed=42)
    print(f"train: {len(split['train'])}, eval: {len(split['test'])}")

    # Quantization config
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
    )

    # Load model
    print(f"loading model: {args.model}")
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
    )
    model = prepare_model_for_kbit_training(model)

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # LoRA config
    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # Training
    training_args = TrainingArguments(
        output_dir=args.output,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=4,
        learning_rate=args.lr,
        fp16=True,
        logging_steps=10,
        eval_strategy="steps",
        eval_steps=50,
        save_strategy="steps",
        save_steps=100,
        warmup_ratio=0.05,
        lr_scheduler_type="cosine",
        report_to="none",
    )

    trainer = SFTTrainer(
        model=model,
        train_dataset=split["train"],
        eval_dataset=split["test"],
        tokenizer=tokenizer,
        args=training_args,
        max_seq_length=args.max_len,
    )

    print("starting training...")
    trainer.train()

    # Save adapter
    model.save_pretrained(args.output)
    tokenizer.save_pretrained(args.output)
    print(f"adapter saved to {args.output}")


if __name__ == "__main__":
    main()
