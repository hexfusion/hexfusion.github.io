---
title: "Disaggregated Prefill/Decode on Consumer GPUs"
date: 2026-03-14
draft: false
tags: ["llm-d", "inference", "rdma", "vllm", "gpu", "bare-metal"]
summary: "Running llm-d's disaggregated prefill/decode architecture across an RTX 3060 and a Tesla T4 connected by 25GbE RDMA. What worked, what broke, and what I learned about KV cache transfer at the edge of what consumer hardware can do."
---

I've been building servers since my twenties. Back in the early 2000s, before cloud as we know it today was a thing, my first setup was a pair of old Intel 1U servers in my basement. Nothing special, just enough to build and host websites and learn how services actually worked. When you do things this way, you can't help but learn all of the components.

That instinct, to understand systems by *building* them, is what led me to set up a bare-metal llm-d lab on hardware I could actually afford. Most of the llm-d ecosystem runs on H100 clusters in cloud data centers. My question was simple: how can you maximize inference using the hardware you already have?

## The Goal

Get a bare-minimum working example of llm-d's disaggregated prefill/decode architecture with RDMA-based KV cache transfer between two physical nodes. Not a benchmark. Not a production deployment. Just proof that the full stack works end-to-end on hardware you can buy on eBay.

## Choosing the Hardware

### The NICs: ConnectX-4 Lx

RDMA was the non-negotiable starting point. llm-d's disaggregated architecture transfers KV cache between prefill and decode pods over the network, and RDMA is what makes that transfer fast enough to be worthwhile. Without RDMA, you're shipping gigabytes of KV cache over TCP, and the network overhead eats any benefit from disaggregation.

I went with Mellanox ConnectX-4 Lx (MCX4111A-ACAT), single-port 25GbE SFP28 cards. They're the sweet spot for a home lab: cheap on the secondary market (~$30-40 each), well-supported by the inbox `mlx5_core` driver (no OFED install needed), and they do RoCE v2 out of the box. I connected them with a direct-attach copper (DAC) cable, no switch needed for two nodes.

### The GPUs: What I Had and What I Could Justify

**RTX 3060 (12GB, Ampere)** was already in my workstation. Consumer card, no NVLink, no MIG, but 12GB is enough for a quantized 7B model with room for KV cache.

**Tesla T4 (16GB, Turing)** was the datacenter GPU I wanted to explore. At current used pricing, it's the most accessible way to get your hands on actual datacenter inference hardware. 16GB VRAM, 70W TDP, designed for inference workloads. I wanted to understand the difference between consumer and datacenter GPUs firsthand, not just read about it. One thing to know about running a T4 in a tower chassis: it's a passively cooled card designed for server airflow. In a workstation without front-to-back forced air, it will throttle. I added extra case fans pointed directly at the card to keep it under thermal limits.

The T4 was a personal choice, not a requirement. Everything in this post could be done with two consumer GPUs. Any sm_75+ card with enough VRAM for your quantized model works fine. A used RTX 2080 Ti (~$200-300) would do the same job for the prefill role, and you wouldn't need the extra cooling.

**GTX 1080 Ti (11GB, Pascal)** was already in the second server. Immediately excluded from the cluster because AWQ quantization requires sm_75 or higher, and the 1080 Ti is sm_61. I disabled it at the PCI bus level so Kubernetes wouldn't try to schedule on it.

### The Servers

Both are Supermicro dual-socket boards I've accumulated over the years:

- **endor** (decode): Supermicro H8DG6, dual AMD Opteron 6272 (32 cores), 92GB RAM, RTX 3060
- **dagobah** (prefill): Supermicro X10DRG-OT+-CPU, dual Xeon E5-2699A v4 (88 threads), 220GB RAM, T4

Old enterprise hardware, but more than enough for this purpose. The CPUs and RAM are irrelevant for inference, the GPU is what matters. The extra RAM on dagobah is consumed by OpenShift VMs running on the same box.

Even a "budget" lab isn't cheap. A used T4 runs ~$700, two ConnectX-4 NICs and a DAC cable another ~$100, and the servers themselves were accumulated over years. Starting from nothing, you're looking at $1000-1500. For pure compute, cloud spot instances are probably cheaper at this scale. The reason to own the hardware isn't cost, it's access. You can't debug RDMA GID tables on a managed cloud instance. You can't learn how RoCE works on GKE. The value is the operational experience you can't get any other way. Simulators get you part of the way, but the real learning happens when things fail in ways no simulator models. Empty GID tables, missing kernel modules, attention backend mismatches that produce garbage instead of errors. Those lessons only happen on real hardware. And demand for this gear is strong, so you can sell it for close to what you paid when you're done. Think of it as renting with no monthly fee.

## Making RDMA Work

### The Easy Part

Install the ConnectX-4 cards, connect with the DAC cable, assign IPs:

```
endor:    10.0.0.1/24 on enp65s0np0 (mlx5_0)
dagobah: 10.0.0.2/24 on ens1np0    (mlx5_0)
```

Verify with `ib_send_bw`:

```
 #bytes   #iterations   BW peak[MB/sec]   BW average[MB/sec]
 65536    5000          2893.47           2893.14
```

23.14 Gbps, that's 92.5% of the theoretical 25GbE line rate. Latency: 1.27 microseconds. The inbox kernel driver just works on Fedora. No OFED, no firmware flashing, no drama.

### The Not-Easy Part

Getting RDMA to work *inside Kubernetes pods* is where it got interesting.

**Problem 1: ib_umad.** The RDMA device plugin (`k8s-rdma-shared-dev-plugin`) couldn't discover devices because the `ib_umad` kernel module wasn't loaded. No error message, just silent failure. Devices showed as 0 allocatable. Fix: `modprobe ib_umad` and persist via `/etc/modules-load.d/`.

**Problem 2: hostNetwork.** The device plugin gives pods access to `/dev/infiniband/` devices, but not the network routing needed to actually *use* them. The RDMA traffic needs to flow over the host's physical NIC, not through a CNI virtual network. Fix: `hostNetwork: true` on both vLLM pods, plus `dnsPolicy: ClusterFirstWithHostNet` so DNS still works.

**Problem 3: GID table.** The RDMA NIC on dagobah had no IP configured at boot. It would get assigned later by my scripts. But RoCE v2 needs an IP in the GID table to function. An empty GID table means the NIC falls back to RoCE v1, and when one side is v2 and the other is v1, the NIXL handshake fails silently. Fix: configure the IP persistently via NetworkManager so it's there before any pods start.

**Problem 4: UCX device naming.** NIXL uses UCX under the hood for RDMA transport. The environment variable `UCX_NET_DEVICES` needs the *InfiniBand* device name (`mlx5_0:1`), not the host network device name (`ens1np0`). Get it wrong and UCX falls back to TCP with no warning. Thankfully, UCX lists available devices in its error output when the specified device doesn't exist.

Every one of these issues took a fair amount of time to debug. None of them are documented in the llm-d getting-started guides, because those guides assume you're on a cloud provider where the RDMA infrastructure is pre-configured. But every one of these issues will hit any team deploying llm-d on bare metal or on-prem.

## Building the Kubernetes Layer

k3s on both nodes. Lightweight, single-binary, gets out of the way. The control plane runs on endor, and dagobah joins as an agent.

```
endor:    k3s server (control plane + decode workloads)
dagobah: k3s agent (prefill workloads)
```

NVIDIA GPU Operator exposes the GPUs. The key setting: `driver.enabled=false` because both nodes already have the NVIDIA driver installed at the host level (akmod-nvidia on Fedora). The operator just needs to install the container toolkit, not the driver.

One gotcha that cost me an afternoon: the upstream vLLM container image (`vllm/vllm-openai`) ships CUDA 12.9. My driver is 580.x which provides CUDA 13.0. On datacenter GPUs, NVIDIA's forward-compatibility libraries handle this mismatch. On consumer GPUs like the RTX 3060, they don't. I had to build a custom image based on `nvidia/cuda:13.0.1-devel-ubuntu24.04` with vLLM installed from PyPI.

The `devel` base (not `runtime`) matters too. FlashInfer needs `nvcc` for JIT compilation on the T4's sm_75 architecture. Precompiled kernels only exist for sm_80+.

## The Gateway Stack

llm-d uses the Kubernetes Gateway API with an inference extension. The stack:

```
Client -> agentgateway (Envoy) -> ext-proc -> EPP (scheduler) -> vLLM pod
```

The EPP (Endpoint Picker) is the brain. It decides which pod should handle each request based on KV cache state, queue depth, and pod roles. For disaggregated P/D, it runs two scheduling passes: pick the decode pod first, then decide if a separate prefill pod is needed.

I hit one naming evolution that caused confusion: `kgateway` was rebranded to `agentgateway` between releases. The kgateway v2.2.0 image I initially deployed had the inference extension disabled by default, with an env var (`KGW_ENABLE_AGENTGATEWAY`) that required CRDs from a domain (`agentgateway.dev`) that didn't exist yet in that release. Switching to the agentgateway chart (v1.0.0-alpha.4) fixed everything.

## Disaggregated Prefill/Decode

This is the payoff. The core idea: prefill (processing the input prompt) is compute-bound and benefits from a strong GPU. Decode (generating output tokens one at a time) is memory-bandwidth-bound. By splitting them across specialized pods, you can optimize each independently.

In my lab:
- **T4 (dagobah) for prefill.** 16GB VRAM, decent tensor core throughput for INT8. Processes the input prompt and builds the KV cache.
- **RTX 3060 (endor) for decode.** 12GB VRAM, 360 GB/s memory bandwidth. Generates output tokens using the transferred KV cache.

The KV cache transfer happens over RDMA via NIXL (NVIDIA's Inference Xfer Library). After the prefill pod processes the prompt, it tells the decode pod "here are the KV cache block IDs." The decode pod's routing sidecar triggers a NIXL transfer, moving GPU memory from dagobah to endor over the 25GbE RDMA link.

### The Attention Backend Trap

This one was subtle and took the longest to debug.

The T4 (sm_75) can't run FlashAttention2, which requires sm_80+. The RTX 3060 (sm_86) picks FA2 by default. When each pod uses a different attention backend, they produce KV caches with *different shapes*, meaning different memory layouts for the key and value tensors.

NIXL transfers raw bytes. It doesn't know or care about tensor shapes. So the transfer succeeds, but the decode pod interprets the bytes with the wrong layout, and inference produces garbage.

The fix: force `--attention-backend FLASHINFER` on both pods. FlashInfer supports both sm_75 and sm_86 (via JIT on the T4), producing compatible KV cache layouts.

This is the kind of issue that never appears in homogeneous cloud deployments where every GPU is the same model. Mixed GPU architectures surface it immediately.

### Results

```
Model:          Qwen2.5-7B-Instruct-AWQ (awq_marlin, max-model-len=1024)
Cache hit rate: 100% external prefix cache hit rate
NIXL transfer:  avg 19.5ms, 36% of small transfers <5ms (RDMA zero-copy)
Peak decode:    96.8 tokens/sec
```

The numbers aren't impressive by datacenter standards. That's not the point. The point is that the full disaggregated P/D stack, agentgateway, EPP with PdProfileHandler, NIXL over RDMA, routing sidecar, and prefix cache scoring, all works end-to-end on two servers that cost less than a single H100.

## Maximizing Older Hardware

Not everyone has access to H100s. Most people getting into this space, whether they're junior engineers, hobbyists, or teams at companies that aren't hyperscalers, are working with whatever GPUs they can get. That doesn't mean they can't participate.

A few things that make older and consumer hardware viable for real inference work:

**Quantization is the great equalizer.** AWQ at INT4 cuts a 7B model from 14GB to ~3.5GB. That's the difference between "doesn't fit" and "fits comfortably with room for KV cache." The quality trade-off at INT4 is surprisingly small for most tasks. If you have an sm_75+ GPU with 8GB or more VRAM, you can serve a useful model.

**Disaggregation helps small GPUs more than big ones.** An H100 with 80GB can hold a 70B model and a huge KV cache at the same time. An RTX 3060 with 12GB can barely fit a 7B model, leaving almost no room for KV cache. By offloading prefill to another node, the decode GPU doesn't need to store both the model and the full prompt's KV cache simultaneously. Disaggregation is more valuable at the margins.

**CPU memory is cheap, GPU memory isn't.** Both of my servers have 92GB and 220GB of RAM. vLLM supports CPU offloading for KV cache, spilling cache pages to system memory when the GPU fills up. It's slower than GPU memory, but it extends your effective context length significantly. If you're running on consumer hardware, this is the first knob to turn.

**The best way to learn distributed systems is to build one.** You don't need expensive hardware to learn how RDMA works, how KV cache transfer affects latency, or how a scheduler makes routing decisions. A $30 NIC and a used datacenter GPU will teach you the same concepts that apply at H200 scale. The debugging skills transfer directly. The failure modes are the same, just at different throughput.

## Why Build It Yourself

I've found that the most effective open source contributions come from solving your own problems. When you use the software daily, you discover the sharp edges and missing pieces naturally. You don't need to go looking for issues to file, they find you.

This lab exists because I wanted to understand llm-d from the inside out. Every RDMA gotcha I documented, every attention backend mismatch I debugged, every EPP configuration quirk I worked around, these are all potential upstream contributions. Not because I set out to contribute, but because I set out to make it work for *me*.

If you're a younger developer looking to get into infrastructure or distributed systems: build something. Don't wait for access to production hardware. Don't wait for a team to give you a project. Get some old servers, install the software, break things, fix them, and write about what you learned. The knowledge you build this way is more durable than anything you'll get from documentation alone.

## What I Learned

**RDMA on bare metal is the hard part.** The inference stack (vLLM, EPP, gateway) is well-documented. The RDMA plumbing (device plugins, GID tables, UCX configuration, host networking) is not. Every bare-metal or on-prem deployment will hit these issues.

**Mixed GPU architectures expose real bugs.** The attention backend mismatch would never surface in a homogeneous cluster. If llm-d wants to support on-prem deployments where hardware isn't uniform, this class of compatibility issue needs attention.

**Consumer GPUs work fine for learning.** The RTX 3060 and T4 aren't fast enough for production inference, but they're fast enough to validate architecture, debug RDMA issues, and understand the full request flow. Every debugging technique I developed here applies directly to an H200 cluster.

**The operational knowledge transfers.** When I eventually get access to datacenter hardware, I won't be figuring out RDMA for the first time. I'll be tuning it.

## What's Next

- Deploy the KV Cache Indexer to enable prefix-aware routing (currently the EPP uses simpler scoring without block-level cache tracking)
- Benchmark baseline (both pods running full P+D) vs disaggregated (split roles)
- Profile RDMA bandwidth utilization during cache transfer to see how close to the 25GbE line rate we actually get under real workloads
- Test with larger context lengths once H200 access is available
- Explore how to make this small model punch above its weight through fine-tuning and LoRA adapters

---

*The lab setup details, including manifests and quickstart guides, are in my [lab notes](https://github.com/hexfusion/design/tree/main/work/llm-d/lab). I plan to open-source the relevant bits once the setup stabilizes.*

*I'm joining the [llm-d](https://github.com/llm-d) team at Red Hat. These are my notes from the onboarding process.*
