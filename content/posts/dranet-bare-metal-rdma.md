---
title: "DRANet: the fix for bare metal RDMA in Kubernetes"
date: 2026-03-17
draft: false
tags: ["llm-d", "rdma", "kubernetes", "dranet", "dra", "bare-metal", "networking"]
summary: "hostNetwork is the default recommendation for RDMA in Kubernetes. It breaks disaggregated inference. DRANet replaces it with DRA-based NIC assignment and fixes the problem cleanly."
---

## Quick Architecture Reference

```
                    hostNetwork (before)                    DRANet (after)
                    ────────────────────                    ──────────────
  Node IP:          192.168.1.233                           192.168.1.233
  Prefill pod IP:   192.168.1.233 (shared!)                 10.42.1.62 (own CNI IP)
  Decode pod IP:    192.168.1.233 (shared!)                 10.42.0.40 (own CNI IP)
  Prefill port:     8300 (remapped to avoid conflict)       8000
  Decode port:      8000                                    8000
  EPP sees:         1 IP, 2 ports → picks one → bug         2 IPs, same port → routes both

  RDMA path:        host namespace (shared)                 pod namespace (moved by DRA)
  RDMA device:      /dev/infiniband/ via device plugin      /dev/infiniband/ via DRANet+NRI
  Network model:    broken (bypasses CNI)                   standard (CNI + DRA side-by-side)
```

**Key concepts to know:**
- **DRA (Dynamic Resource Allocation):** K8s API for requesting hardware (like GPUs, NICs) as schedulable resources. GA in 1.34.
- **NRI (Node Resource Interface):** containerd hook that lets DRANet inject into pod creation to move NICs.
- **ResourceSlice:** How DRANet publishes discovered NICs to the cluster (auto-populated).
- **DeviceClass:** CEL-based selector for which devices pods can claim (like StorageClass but for devices).
- **ResourceClaim:** A pod's request for a specific device, with optional config (IP, MTU, etc).
- **RDMA netns shared mode:** RDMA link device stays on host but is visible from pod. Both namespaces can use it. No exclusive locking needed for single-pod-per-NIC setups.

---

In my [previous post](/posts/disaggregated-pd-consumer-gpus/) I built a bare metal llm-d lab. Two nodes, a T4 and an RTX 3060, connected with 25GbE Mellanox ConnectX-4 Lx NICs over a direct DAC cable. Disaggregated prefill/decode inference with KV cache transfer over RDMA.

I got it working. Then I hit a wall.

## The hostNetwork Trap

The RDMA device plugin gives your pod `/dev/infiniband/` access, but it doesn't give you network routing to the RDMA NIC. hostNetwork does both.

So I did what the guides said. And it worked for a single pod. The moment I deployed disaggregated prefill/decode with two pods on different nodes, the EPP (Endpoint Picker) scheduler silently dropped my prefill pod. Requests only ever hit decode. No errors, no warnings, just... silence.

This is [llm-d#632](https://github.com/llm-d/llm-d/issues/632). The root cause: hostNetwork forces all pods on a node to share the host IP. When you have prefill and decode both wanting port 8000, you have to remap one of them. But the EPP's InferencePool only expects one target port. Change prefill to 8300? EPP ignores it. Add multiple targetPorts? EPP picks one. There's no clean workaround.

The fundamental issue is that hostNetwork breaks the pod networking model that everything else in the stack assumes.

## DRANet: Just Use DRA

[DRANet](https://github.com/kubernetes-sigs/dranet) is a kubernetes-sigs project that takes a completely different approach. Instead of giving your pod the host's network stack, it uses Kubernetes Dynamic Resource Allocation (DRA) to move the physical RDMA NIC into the pod's own network namespace.

Each pod gets:
- Its own CNI IP (normal pod networking, nothing special)
- The physical RDMA NIC with its own IP, moved in by DRANet
- Full RDMA device access (mlx5_0, uverbs, the works)

No hostNetwork. No port conflicts. No EPP hacks.

## The Migration

My lab runs k3s v1.34 on Fedora. Here's what the migration looked like.

### What I removed

```yaml
# no longer in pod spec
hostNetwork: true
dnsPolicy: ClusterFirstWithHostNet
resources:
  limits:
    rdma/hca_shared_devices_a: "1"
```

The entire `k8s-rdma-shared-dev-plugin` DaemonSet, deleted.

### What I added

DRANet, one DaemonSet install:

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/dranet/refs/heads/main/install.yaml
```

It discovers your NICs automatically. Within seconds, `kubectl get resourceslices` showed both my Mellanox cards with their IPs, PCI addresses, and RDMA capability.

A DeviceClass to select RDMA-capable NICs:

```yaml
apiVersion: resource.k8s.io/v1
kind: DeviceClass
metadata:
  name: rdma-net
spec:
  selectors:
    - cel:
        expression: device.driver == "dra.net"
    - cel:
        expression: device.attributes["dra.net"].rdma == true
    - cel:
        expression: device.attributes["dra.net"].virtual == false
```

ResourceClaims for each pod, selecting the specific NIC and configuring its IP:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaim
metadata:
  name: rdma-prefill
spec:
  devices:
    requests:
    - name: rdma-nic
      exactly:
        deviceClassName: rdma-net
        selectors:
        - cel:
            expression: device.attributes["dra.net"].ifName == "ens1np0"
    config:
    - opaque:
        driver: dra.net
        parameters:
          interface:
            addresses:
            - "10.0.0.2/24"
```

Pod spec references the claim:

```yaml
spec:
  containers:
  - name: vllm
    resources:
      limits:
        nvidia.com/gpu: "1"
      claims:
      - name: rdma
    securityContext:
      capabilities:
        add: ["IPC_LOCK"]
  resourceClaims:
  - name: rdma
    resourceClaimName: rdma-prefill
```

That's it. Both pods listen on port 8000. No remapping.

### What it looks like inside the pod

```
$ kubectl exec deploy/vllm-prefill -- ip addr show
2: eth0@if19: <BROADCAST,MULTICAST,UP,LOWER_UP>
    inet 10.42.1.62/24          # CNI IP, normal pod networking
5: ens1np0: <BROADCAST,MULTICAST,UP,LOWER_UP>
    inet 10.0.0.2/24            # Physical RDMA NIC, moved in by DRANet

$ kubectl exec deploy/vllm-prefill -- rdma link show
link mlx5_0/1 state ACTIVE physical_state LINK_UP netdev ens1np0
```

The pod has its own IP for service traffic and the physical RDMA NIC for zero-copy KV cache transfer. NIXL/UCX uses `mlx5_0:1` for RDMA just like before. Nothing changes on the application side.

## The Result

Sent a request through the gateway. EPP routed it to the decode pod. Decode coordinated with prefill. Prefill did the compute, transferred the KV cache over RDMA, decode generated tokens.

```
External prefix cache hit rate: 100.0%
KV Transfer metrics: Avg xfer time (ms)=18.596, Throughput (MB/s)=94.106
```

Both pods received traffic. The bug is gone.

## Prerequisites

A few things that need to be in place and survive reboot:

- **RDMA kernel modules**: `/etc/modules-load.d/rdma.conf` with `rdma_cm`, `rdma_ucm`, `ib_umad`
- **RDMA NIC IPs via NetworkManager**: persistent connections with `autoconnect=yes` (DRANet configures the IP inside the pod, but NM handles it when no pod is running)
- **NRI**: containerd 2.x has it enabled by default. DRANet uses NRI to hook into pod creation
- **DRA**: GA in Kubernetes 1.34+, feature-gated in 1.32-1.33

## Why This Matters

The hostNetwork + device plugin approach is what every bare metal RDMA guide recommends today. It works for single pods. It breaks the moment you need multiple pods with different ports on the same node, which is exactly what disaggregated inference requires.

DRANet is the upstream answer. It's a kubernetes-sigs project, it works with existing CNI plugins, and it treats RDMA NICs as schedulable resources instead of a networking hack. For anyone building llm-d on bare metal, this is the path forward.
