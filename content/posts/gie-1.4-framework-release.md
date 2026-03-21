---
title: "GIE 1.4: the framework release (and what it means for llm-d)"
date: 2026-03-21
draft: false
tags: ["llm-d", "kubernetes", "gie", "inference", "gateway-api"]
summary: "Gateway API Inference Extension v1.4 landed with 101 commits from 54 contributors. The headline isn't a single feature, it's that GIE became a real plugin framework. Here's what changed and why it matters if you're building on top of it."
---

[Gateway API Inference Extension](https://github.com/kubernetes-sigs/gateway-api-inference-extension) v1.4.0 shipped on March 20 with 101 commits from 54 contributors, 13 of them first-timers! I've been studying GIE internals for the past few weeks as part of onboarding to [llm-d](https://github.com/llm-d), which builds its inference scheduler on top of GIE's Endpoint Picker (EPP). So I've been watching this release closely.

## What is GIE?

Quick context if you're new to this ecosystem. GIE is a Kubernetes SIG project (`kubernetes-sigs/gateway-api-inference-extension`) that turns any ext-proc capable proxy, think Envoy Gateway or kgateway, into an inference-optimized load balancer for self-hosted LLMs. The core component is EPP, the Endpoint Picker. It sits behind Envoy as an [ext-proc filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_proc_filter), intercepts every request, parses the model name from the JSON body, and runs a Filter -> Score -> Pick scheduling pipeline to choose the best backend pod. The design is modeled after kube-scheduler's plugin framework.

## The framework reorg

The most structurally significant change in 1.4 is the codebase reorganization. All plugin interfaces and scheduling types moved under `epp/framework/`, with strict import confinement enforced by a [validation script](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2337). Scheduling plugins, request control plugins, and data layer plugins each got their own subdirectory under `epp/framework/plugins/`.

This sounds like housekeeping, but it's not. Before this change, plugin interfaces were scattered across packages and it wasn't always clear what a downstream consumer (like llm-d) should import vs what was internal. Now there's a clean boundary: `epp/framework/interface/` is the stable surface, everything else is implementation detail. I traced this through PRs [#2195](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2195), [#2192](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2192), [#2230](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2230), and [#2286](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2286) to see the progression.

A related change: scorer weights changed from `int` to `float` in [#2207](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2207). Small API change, but it means you can now express finer-grained weight ratios between scorers without integer rounding.

## Standalone EPP

EPP can now be deployed as its own Helm chart, independent of the InferencePool chart. The work landed across several PRs starting with [#2122](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2122), with a [user guide](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2147) and resource configuration support in [#2273](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2273).

This matters for anyone running EPP with custom plugins. Previously you had to deploy the full InferencePool chart even if all you wanted was your own EPP binary with custom scorers registered. Now you can build your own EPP image, point the standalone chart at it, and deploy it next to an InferencePool managed separately.

## Pluggable BBR

The Body-Based Router (BBR) is a separate ext-proc server that runs before EPP. Its job is simple: parse the HTTP body, extract the `model` field, and write it as a header so the gateway can route by model at the header level. In 1.3 this was a fixed implementation.

In 1.4, BBR got a plugin framework of its own ([#2121](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2121), [#2209](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2209)). There's now a configurable body-fields-to-headers plugin ([#2417](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2417)) so you can extract arbitrary fields from the request body and promote them to headers for routing decisions.

The request and response paths were also refactored to accept raw bytes and support pluggable parsers ([#2409](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2409), [#2410](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2410)). This is groundwork for the gRPC support that's coming (the API piece already landed, see below).

## Data layer refactoring

The data layer, which is how EPP collects pod metrics and state, got a significant overhaul. The HTTP datasource was refactored in [#2120](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2120), and a new distinction was introduced between polling-based and notification-based data sources ([#2320](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2320), [#2407](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2407)).

Before 1.4, all data collection was HTTP polling: EPP scrapes each pod's Prometheus `/metrics` endpoint on an interval. Now there's a clean interface for notification-based sources too, like Kubernetes watch events. Plugin execution order across layers is validated at startup ([#2333](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2333)), which catches misconfiguration early.

This aligns with what was proposed in [Proposal 1023 (Data Layer Architecture)](https://github.com/kubernetes-sigs/gateway-api-inference-extension/tree/main/docs/proposals/1023-data-layer-architecture). The data layer is now genuinely pluggable rather than hardcoded to HTTP metric scraping.

## Flow control maturity

Flow control, the priority and fairness system that decides what happens under overload, got several important changes:

- **Priority band garbage collection** ([#2097](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2097)) so empty priority bands don't accumulate forever
- **Concurrency saturation detection** ([#2062](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2062)) as a new signal for when endpoints are overloaded
- **FailOpen as the default** ([#2365](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2365)) on InferencePool, meaning if EPP is down the gateway forwards traffic to backends directly instead of failing requests
- **Fairness and ordering policy migration** ([#2188](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2188), [#2193](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2193)) with the `interflow` package renamed to `fairness` and `intraflow` to `ordering`, which makes the code much easier to read

The FailOpen default is worth highlighting. In 1.3, if EPP went down, all inference traffic stopped. That's the right default for safety during development, but the wrong default for production. Flipping to FailOpen means a crashed or restarting EPP doesn't take down your inference endpoint. You lose smart routing but keep serving.

## gRPC and multimodal

Two API-level additions worth noting:

**gRPC support** landed via `appProtocol` on InferencePool ([#2162](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2162)) with ALPN h2 support for TLS ([#2385](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2385)). This is the API piece of [Proposal 2162](https://github.com/kubernetes-sigs/gateway-api-inference-extension/tree/main/docs/proposals/2162-grpc-support). The full implementation (gRPC-to-gRPC routing, HTTP-to-gRPC transcoding) is coming in phases. Both vLLM and SGLang expose gRPC endpoints, so this opens the door to binary-framed inference with lower overhead than JSON.

**Multimodal inputs** now include video and audio format support ([#2181](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2181)), alongside the existing image support. Plus the **Responses API and Conversations API** ([#2133](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2133)) for alternative OpenAI-compatible endpoints.

## Latency prediction gets PD-aware

The predicted latency scorer (renamed from "slo-aware-router" in [#2183](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2183)) now understands prefill/decode disaggregation ([#2361](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2361)). It can make different latency predictions for prefill pods vs decode pods, and handles disaggregated mode filtering in [#2390](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2390).

Latency prediction also moved from scoring time to the PrepareData step ([#2319](https://github.com/kubernetes-sigs/gateway-api-inference-extension/pull/2319)), which means predictions are computed once and shared across scoring plugins via CycleState rather than recomputed by each scorer that needs them.

## What this means for llm-d

I'm onbaording into the [llm-d](https://github.com/llm-d) team, which builds a disaggregated inference framework on top of GIE. llm-d's [inference scheduler](https://github.com/llm-d/llm-d-inference-scheduler) extends GIE's plugin system with custom filters (`decode-filter`, `prefill-filter`), scorers (`prefix-cache-scorer`, `load-aware-scorer`), and profile handlers (`pd-profile-handler`) that implement prefill/decode separation. Here's what 1.4 means for that stack.

**The framework reorg is the biggest deal.** llm-d imports GIE's plugin interfaces to register its own scorers and filters. A clean `epp/framework/interface/` boundary means llm-d can pin to a stable API surface instead of reaching into internal packages. This should reduce breakage on GIE version bumps, which has been a real friction point.

**Standalone EPP unlocks cleaner deployment.** llm-d already builds its own EPP binary with custom plugins registered at build time. The standalone chart means llm-d doesn't need to fork the InferencePool Helm chart just to swap in its EPP image. Deploy InferencePool for your model server pods, deploy standalone EPP with llm-d's plugins separately.

**Scorer weight floats help.** llm-d runs multiple scorers in its scheduling profiles: prefix cache affinity, load-aware distribution, KV cache utilization. Tuning the balance between these with integer weights was coarse. Float weights let you express things like "prefix cache affinity matters 1.5x more than load distribution" without scaling everything up.

**PD-aware latency prediction is directly relevant.** llm-d's whole architecture is built around disaggregated prefill and decode. Having the latency predictor understand that prefill pods and decode pods have fundamentally different latency characteristics means this scorer becomes useful for llm-d deployments out of the box, instead of needing a custom replacement.

**Pluggable data layer means custom metrics.** llm-d's vLLM pods expose metrics beyond what the standard model server protocol requires, things like NIXL transfer latency and per-adapter cache hit rates. The new notification-based data source interface could let llm-d push metrics to EPP via watch events rather than relying solely on HTTP polling, which would reduce metric staleness for fast-moving state like KV cache occupancy.

**FailOpen default is the right call for production.** llm-d deployments are typically multi-pod with both prefill and decode pools. A crashed EPP shouldn't stop all inference. With FailOpen, traffic falls back to round-robin until EPP recovers. You lose cache-aware routing temporarily, but you keep serving.

## The bigger picture

The pattern I see: GIE is following trajectory similar to kube-scheduler. Start with a monolithic implementation, identify the extension points, formalize them as plugin interfaces, enforce boundaries so plugins don't accidentally depend on internals, then let the ecosystem build on top. It took kube-scheduler several releases to get the plugin framework right. GIE is doing it faster, probably because the pattern isn't new.

## References

- [GIE v1.4.0 release](https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/tag/v1.4.0)
- [GIE docs](https://gateway-api-inference-extension.sigs.k8s.io/)
