---
title: "Fine-tuning a Go expert: LoRA on a $300 GPU (Part 1)"
date: 2026-03-18
draft: false
tags: ["llm-d", "lora", "fine-tuning", "kubernetes", "go", "gpu", "machine-learning"]
summary: "I trained a LoRA adapter on 41k Go code examples from the Kubernetes and etcd source trees. The first run produced 600 tab characters. Here's what I learned."
---

## Quick Reference

**What is LoRA?**
LoRA (Low-Rank Adaptation) is a technique for fine-tuning large language models without retraining all their weights. Instead of updating 7 billion parameters, you inject small trainable matrices into the model's attention layers and train those. The result is an adapter -- think of it like a `git diff` for the model. The base model (~15GB) stays frozen; the adapter (~20MB) patches its behavior for your domain.

**What is QLoRA?**
QLoRA combines LoRA with 4-bit quantization. You load the base model in 4-bit which was ~4GB for 7B params to fit in GPU memory, then train the LoRA adapters at full precision. This is was what I had to do to fine-tune the 7B model possible on a 16GB GPU.

**Adapter vs model:**
The adapter is not a new model. It's more like a git diff. To serve it, you load the base model and apply the adapter on top. vLLM supports this natively you can serve the base model and multiple adapters from a single GPU, switching per-request by model name.

---

This is the third post in my series on building a bare metal llm-d lab. In [part 1](/posts/disaggregated-pd-consumer-gpus/) I set up disaggregated prefill/decode inference. In [part 2](/posts/dranet-bare-metal-rdma/) I replaced `hostNetwork` with DRANet to fix EPP routing. Now I want to train a domain-specific adapter and serve it on the same stack.

The goal: a model that's better at Go distributed systems patterns. Controller loops, Raft consensus, gRPC, operator patterns. The kind of code that lives in `kubernetes/kubernetes`, `etcd-io/etcd`, and `cockroachdb/cockroach`.

## The Data

Before I could train anything, I needed training data. I looked into the formats used for code fine-tuning and landed on instruction/output pairs a natural language description and the code that answers it.

For sources I picked some of my favorite Go projects the repos I've spent the most time reading and learning from. I figured if I want the model to write code that looks like good Go, these are probably the right teachers.

I wrote a Go tool that walks a source tree, parses every `.go` file with `go/ast`, and extracts functions that have doc comments. The comment becomes the instruction; the function body becomes the output.

```go
// source repos
repos := []string{
    "repos/kubernetes",
    "repos/etcd",
    "repos/grpc-go",
    "repos/containerd",
    "repos/consul",
    "repos/cockroach",
    "repos/stdlib",
}
```

This produced **41,805 pairs** across seven repos: 14k from Go stdlib, 10k each from cockroach and kubernetes, and the rest from etcd, consul, grpc-go, and containerd.

Each pair looks like:

```jsonl
{
  "instruction": "reconcileHandler processes a work item from the queue...",
  "output": "func (c *Controller) reconcileHandler(...) {\n\t..."
}
```

I skipped manual review and set the quality bar at "has a doc comment and is at least 5 lines." There's noise in there, but from what I read, fine-tuning tends to be fairly forgiving of noisy data when you have enough of it.

## The Hardware

Training node: **dagobah** an old Xeon workstation with a Tesla T4 16GB GPU. The T4 is a datacenter card from 2018, built for inference. I figured it would be fine for training too. That assumption got tested and was mostly wrong heh.

Serving: the existing llm-d P/D stack from the previous posts. Prefill on the T4, decode on an RTX 3060.

## Training Setup

I went with `Qwen/Qwen2.5-7B-Instruct` as the base the non-quantized version. More on why not the AWQ version in a moment.

For the training stack I used [PEFT](https://github.com/huggingface/peft) for LoRA, [TRL](https://github.com/huggingface/trl) SFTTrainer for the training loop, and [BitsAndBytes](https://github.com/bitsandbytes-foundation/bitsandbytes) for 4-bit quantization. I found this combination through the [Hugging Face QLoRA guide](https://huggingface.co/blog/4bit-transformers-bitsandbytes), which walks through exactly this setup and is where most people seem to start.

LoRA config:
- Rank `r=16`, alpha=32 (standard starting point)
- Target modules: `q_proj`, `k_proj`, `v_proj`, `o_proj` (attention layers)
- ~10M trainable parameters out of 7.6B total about 0.13%

## The Gauntlet

Nothing worked on the first try.

**Attempt 1 AWQ base + BnB 4-bit:**
```
ValueError: You cannot load an AWQ model and quantize it with BitsAndBytes
```
I started with the AWQ model since that's what I was already running in the lab. Turns out AWQ is itself a quantized format you can't quantize it again. Switched to `Qwen2.5-7B-Instruct`, the full-precision base.

**Attempt 2 fp16 AMP training:**
```
NotImplementedError: "_amp_foreach_non_finite_check_and_unscale_cuda"
not implemented for 'BFloat16'
```
After digging into it, I found that the T4 (sm_75) doesn't support bfloat16 natively -- and Qwen2 stores its internal tensors in bf16. PyTorch's AMP gradient scaler hits these during the backward pass and crashes.

I tried casting all model parameters and buffers to fp16 to work around it, but that didn't help. The bf16 turns out to persist inside the loss function itself, not in the model weights. I wasn't able to find a clean fix for this on the T4.

**Attempt 3 CUDA OOM with batch=4:**
```
torch.OutOfMemoryError: CUDA out of memory
```
I wasn't sure why until I thought through the numbers 7B in 4-bit in a batch of 4 sequences pushes past 16GB thanks claude. Dropped to batch=1.

**Attempt 4 fp32, it runs:**
Disabling both fp16 and bf16 (`fp16=False, bf16=False` in the trainer config) forces full fp32 training. The T4 seems to handle fp32 fine. It runs but slowly, around 21 seconds per step.

For a first run I subsampled to 5,000 pairs and ran for one pass through the data. About two hours total.

The SFTTrainer logs progress every few steps and prints a summary when training finishes:

```
{'loss': 7.7646, 'grad_norm': ..., 'learning_rate': ...}
...
{'train_runtime': 7427.0, 'train_samples_per_second': ..., 'train_loss': 7.7646}
```

Final stats: **loss=7.76, token accuracy=4.2%**.

## Serving the Adapter

The adapter came out as a 20MB `adapter_model.safetensors` file. I wasn't sure how to serve it alongside the base model, but vLLM has a `--lora-modules` flag that makes it straightforward:

```yaml
args:
  - --model
  - Qwen/Qwen2.5-7B-Instruct-AWQ
  - --enable-lora
  - --lora-modules
  - go-adapter=/adapter
```

Both show up in `/v1/models`:

```json
{"id": "Qwen/Qwen2.5-7B-Instruct-AWQ", ...},
{"id": "go-adapter", "parent": "Qwen/Qwen2.5-7B-Instruct-AWQ", ...}
```

You switch between them by setting `"model": "go-adapter"` in the request:

```bash
curl http://localhost:8200/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "go-adapter",
    "messages": [{"role": "user", "content": "Write a controller reconcile loop in Go"}],
    "max_tokens": 400
  }'
```

I expected this to need a lot more GPU memory, but the adapter weights are small enough that it wasn't an issue.

## The Test

Same prompt, two models:

```
Write a Kubernetes controller reconcile function in Go
using controller-runtime that handles not-found gracefully.
```

**Base model (`Qwen/Qwen2.5-7B-Instruct-AWQ`):**
```go
instance := &v1.MyResource{}
err := r.Get(ctx, req.NamespacedName, instance)
if err != nil {
    return ctrl.Result{}, client.IgnoreNotFound(err)
}
// ... reconciliation logic
return ctrl.Result{}, nil
```

Looks reasonable to me. The `client.IgnoreNotFound` pattern is what I'd expect, function shape is right.

**Adapter (`go-adapter`):**
```
[600 tab characters]
```

Not a rendering issue. The adapter literally outputs tab characters until it hits the token limit.

## What Went Wrong

I had to think about this for a bit. Go source code is tab-indented throughout every function body, every `if`, every `for` starts with one or more `\t` characters. With only 5k samples and one pass through the data, the adapter didn't seem to learn semantic patterns at all. It learned the most statistically common token in the training data: the tab.

I found out this is called *degenerate collapse*. As best I understand it, the model found a local minimum where "output tabs" scores lower loss than random tokens, so it got stuck there. Getting past it apparently requires enough data that the model is forced to learn something more meaningful.

The training numbers make more sense in hindsight. I read that a well-adapted model should land somewhere around loss 1.5-2.5. At 7.76, the adapter barely moved from its starting point. Token accuracy of 4.2% is close to random the pipeline mostly works, I think the adapter just needs a lot more training to be useful.

## What's Next

My plan is to try the full dataset on the T4 in fp32 first since that's the path I know works. Longer term I want to figure out the fp16 issue properly, or just move training to the RTX 3060 and see if bf16 support there means I don't have to.

Part 2 will cover whichever path wins and what the adapter actually looks like when it works.

## Reproduce It

**Requirements:** NVIDIA GPU (12GB+), Python 3.11, CUDA, Go 1.21+

```bash
pip install 'transformers<5.0' peft trl bitsandbytes datasets torch
```

Training script: [train_qlora.py](/code/lora-go-training/train_qlora.py)

```bash
# v1 run (T4, fp32, 5k subsample)
python train_qlora.py \
  --model Qwen/Qwen2.5-7B-Instruct \
  --data training_data.jsonl \
  --output output/v1 \
  --epochs 1 --max-samples 5000

# Serve adapter
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --enable-lora --lora-modules go-adapter=/path/to/adapter
```

Training data was extracted from public Go repos using `go/ast` to pull every documented function as an instruction/output pair. Any JSONL file with `instruction` and `output` fields works.

## References

- [LoRA: Low-Rank Adaptation of Large Language Models](https://arxiv.org/abs/2106.09685) Hu et al., 2021. The original paper.
- [QLoRA: Efficient Finetuning of Quantized LLMs](https://arxiv.org/abs/2305.14314) Dettmers et al., 2023. How to train on quantized models.
- [Hugging Face QLoRA guide](https://huggingface.co/blog/4bit-transformers-bitsandbytes) Where I found the PEFT + TRL + BitsAndBytes stack.
- [Hugging Face PEFT](https://github.com/huggingface/peft) LoRA implementation used here.
- [TRL SFTTrainer](https://huggingface.co/docs/trl/sft_trainer) Supervised fine-tuning trainer.
- [vLLM LoRA docs](https://docs.vllm.ai/en/latest/features/lora.html) How to serve adapters in vLLM.
