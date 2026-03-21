---
title: "Fine-tuning a Go expert: does it actually work? (Part 2)"
date: 2026-03-19
draft: false
tags: ["llm-d", "lora", "fine-tuning", "kubernetes", "go", "gpu", "machine-learning"]
summary: "The v2 adapter trained overnight on 41k samples. Loss 0.918, accuracy 82.7%. I loaded it into vLLM and ran the same prompts. Here's what came out."
---

This is a follow-up to [Part 1](/posts/lora-go-training-pt1/), where the v1 adapter produced 600 tab characters oops. I moved training to the RTX 3060, hit a few more walls, and eventually got a v2 run to complete.

## v2 Training Setup

After v1 I moved to the RTX 3060 (endor). The goal was to train on the full 41k sample dataset with one pass through the data.

I had to learn what `sm_86` means along the way. NVIDIA assigns every GPU a compute capability version `sm_` stands for streaming multiprocessor, and the number is essentially the GPU's feature set version. The T4 is sm_75 (Turing, 2018). The RTX 3060 is sm_86 (Ampere, 2020). I found the full table in [NVIDIA's CUDA GPU list](https://developer.nvidia.com/cuda-gpus). The reason it matters here: bf16 arithmetic is only natively supported from sm_80 onward, which is why the T4 couldn't use it and the 3060 can.

Getting there took a few more attempts.

**Transformers 5.x OOM:**
The first run failed immediately on model load with an out-of-memory error. Not during training, before a single step ran. I eventually found that transformers 5.x changed how quantized models load. The new code materializes the full model in GPU memory before applying BnB quantization, which blows past 12GB. The fix was to pin to an older version: `pip install 'transformers<5.0'`. I landed on 4.57.6.

**CUDA OOM at step 50:**
The model loaded. Training started. At step 50 it crashed with another OOM. This one was the logits tensor from computing loss over long sequences. I dropped `--max-len` from 512 to 256, but it still crashed. After digging through the [PyTorch CUDA memory management docs](https://pytorch.org/docs/stable/notes/cuda.html) I decided to try `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`. The docs describe it as a mode where the allocator grows segments incrementally rather than pre-reserving large contiguous blocks — which matters when you're carving memory for a large model and a large logits tensor at the same time. That cleared it.

After those two fixes the run went clean.

## The Numbers

v2 trained for **17.4 hours** on the RTX 3060, 2,483 steps, bf16, full 41k dataset.

| | v1 | v2 |
|--|--|--|
| Hardware | T4 (sm_75) | RTX 3060 (sm_86) |
| Dataset | 5k samples | 41k samples |
| Precision | fp32 | bf16 |
| Runtime | ~2h | 17.4h |
| Final loss | 7.76 | **0.918** |
| Token accuracy | 4.2% | **82.7%** |

Loss around 0.9 is in the range I'd expect for a reasonable adapter. The token accuracy jump from 4% to 82% is significant. v1 was basically random. v2 had actually learned something from the training data.

## Loading the Adapter

The adapter came out of training as a directory with `adapter_config.json`, `adapter_model.safetensors`, and the tokenizer files. Total size: around 20MB for the safetensors file, a few KB for config.

I wasn't sure how to get it into the running vLLM instance. I ended up stopping the decode pod, mounting the adapter directory as a hostPath volume, and passing `--enable-lora --lora-modules go-adapter=/adapter` as args to vLLM. When the pod came back up, both the base model and the adapter showed up in `/v1/models`:

```json
{"id": "Qwen/Qwen2.5-7B-Instruct-AWQ", "parent": null, ...},
{"id": "go-adapter", "parent": "Qwen/Qwen2.5-7B-Instruct-AWQ", ...}
```

You switch between them per-request by setting `"model": "go-adapter"` in the request body. The base model is still available at the same endpoint. I hadn't really thought about it before but this is a clean way to A/B test: same server, same GPU, just a different model name.

## The Comparison

Same prompt as Part 1:

```
Write a Kubernetes controller reconcile function in Go
using controller-runtime that handles not-found gracefully.
```

**Base model:**

```go
myCustomRes := &MyCustomResource{}
if err := cl.Get(ctx, req.NamespacedName, myCustomRes); err != nil {
    if apiErr, ok := err.(*client.StatusError); ok && apiErr.ErrStatus.Reason == metav1.StatusReasonNotFound {
        return ctrl.Result{}, nil
    }
    return ctrl.Result{}, fmt.Errorf("error getting MyCustomResource: %v", err)
}
```

It works but the not-found check isn't quite right. The idiomatic pattern in controller-runtime is `errors.IsNotFound(err)` from `k8s.io/apimachinery/pkg/api/errors`. The base model does a type assertion on `client.StatusError` directly, which is a less portable pattern.

**Adapter (go-adapter v2):**

```go
import (
    "k8s.io/apimachinery/pkg/api/errors"
    ctrl "sigs.k8s.io/controller-runtime"
    "sigs.k8s.io/controller-runtime/pkg/controllerutil"
    "sigs.k8s.io/controller-runtime/pkg/log"
)

//+kubebuilder:rbac:rules=[{apiGroups=[],resources=["pods"],verbs:["get","list","watch"]}]

func (mc *MyController) SetupWithManager(mgr ctrl.Manager) error { ... }

func (mc *MyController) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    logger := log.FromContext(ctx)
    ...
}
```

The adapter reaches for `k8s.io/apimachinery/pkg/api/errors` which is the right package. It also generates `kubebuilder:rbac` markers and `SetupWithManager` patterns that come directly from real controller-runtime codebases. It knows the `log.FromContext(ctx)` idiom.

I tried a few more prompts.

**etcd leader election:**

The base model invented API calls that don't exist in the etcd client library (`etcdClient.Grant`, `etcdClient.Lock` as I tried to call them). The adapter pulled a real import path and attempted a working approach, even if the leader election logic wasn't complete. Neither was production-ready but the adapter at least referenced APIs that actually exist.

**gRPC server streaming:**

Both models struggled here. The server-streaming handler signature in Go (`func (s *server) StreamFoo(req *pb.Req, stream pb.Service_StreamFooServer) error`) is unusual enough that I'd say both got it wrong on the first try. The adapter at least mentioned `stream.CloseSend()` which is a real method. But this was the weakest result of the three tests.

## What Actually Changed

The v1 collapse was about data volume. 5k samples and one epoch isn't enough for the model to learn semantic patterns it finds a local minimum (the most common token: tab) and sticks there.

v2 is better at:
- **Idiomatic Go imports**: it reaches for the right packages (`k8s.io/apimachinery/pkg/api/errors`, `sigs.k8s.io/controller-runtime`) rather than inventing things
- **Controller-runtime patterns**: `SetupWithManager`, `log.FromContext`, `kubebuilder:rbac` markers all showed up unprompted
- **Error handling shape**: wraps errors correctly, returns `ctrl.Result{}` in the right places

It still hallucinates:
- **APIs within packages**: the etcd example used real imports but made up method calls
- **Complex interface implementations**: gRPC streaming signatures were close but wrong
- **Struct fields and config**: details that require having seen the specific package

My read is that 41k samples over one epoch moves the needle on general patterns but isn't enough to reliably reproduce specific API surface. The model learned the *shape* of Go code from these repos more than the exact APIs.

## What's Next

The plan was always to compare this with InstructLab the same domain done the "right" way with taxonomy files and synthetic data generation rather than raw extraction from source trees. That's Phase 2. I don't know yet whether the results will be better or just different.

A few things I'd try before calling this adapter done:
- More epochs on the same data the loss was still declining at the end of v2
- Better data quality I skipped manual review, and there's definitely noise in the 41k pairs
- Targeted prompts in the training set the leader election and gRPC failures suggest those patterns aren't well-represented in the data

Part 3 will cover synthetic data generation using Red Hat's [SDG Hub](https://github.com/Red-Hat-AI-Innovation-Team/sdg_hub) and training with [Training Hub](https://github.com/Red-Hat-AI-Innovation-Team/training_hub).

## Reproduce It

Same training script as Part 1: [train_qlora.py](/code/lora-go-training/train_qlora.py)

```bash
# v2 run (RTX 3060, bf16, full dataset)
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
python train_qlora.py \
  --model Qwen/Qwen2.5-7B-Instruct \
  --data training_data.jsonl \
  --output output/v2 \
  --epochs 1 --max-len 256 --bf16
```

## References

- [QLoRA: Efficient Finetuning of Quantized LLMs](https://arxiv.org/abs/2305.14314): Dettmers et al., 2023
- [BitsAndBytes](https://github.com/bitsandbytes-foundation/bitsandbytes): 4-bit quantization library
- [TRL SFTTrainer](https://huggingface.co/docs/trl/sft_trainer): Supervised fine-tuning trainer
- [vLLM LoRA docs](https://docs.vllm.ai/en/latest/features/lora.html): Serving adapters in vLLM
- [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime): K8s controller library used as training source
