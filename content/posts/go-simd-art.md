---
title: "Go finally gets SIMD in 1.26"
date: 2026-03-20
draft: false
tags: ["go", "simd", "performance"]
summary: "Go 1.26 shipped simd/archsimd behind GOEXPERIMENT=simd, giving Go native SIMD intrinsics for the first time. I tried it on a real data structure to see what it feels like in practice."
---

![Go Gopher](/images/gopher.png)

Go has never had a clean story for SIMD. If you wanted vector instructions, you wrote assembly stubs by hand, used `unsafe` pointer tricks, or accepted the compiler's auto-vectorization (which is conservative at best). Go 1.26 changes that.

## What shipped

The new `simd/archsimd` package is gated behind `GOEXPERIMENT=simd`. I found the design in [proposal #73787](https://github.com/golang/go/issues/73787) and the [package docs](https://pkg.go.dev/simd/archsimd). The API exposes typed SIMD vector types like `Int8x16`, `Float32x4`, and `Int32x4` with methods for arithmetic, comparison, and mask extraction.

```bash
GOEXPERIMENT=simd go build ./...
```

Without the flag, `import "simd/archsimd"` does not resolve. This is intentional since the API is experimental and may change before the flag is removed.

## The API design

The approach is explicit, not magic. There is no auto-vectorization. You tell the compiler exactly what SIMD operations you want.

To show what this means in practice, here is the same operation in C and Go. The goal: given an array of 16 bytes, find which one matches a search byte, using a single SIMD comparison instead of a loop.

**C (SSE2 intrinsics):**

```c
#include <immintrin.h>
#include <stdint.h>

int find_match(uint8_t keys[16], uint8_t search) {
    // Load 16 bytes into a 128-bit register
    __m128i key_vec = _mm_loadu_si128((__m128i*)keys);
    // Fill all 16 lanes with the search byte
    __m128i cmp_vec = _mm_set1_epi8((char)search);
    // Compare all 16 lanes at once
    __m128i result = _mm_cmpeq_epi8(key_vec, cmp_vec);
    // Extract one bit per lane into an integer
    int mask = _mm_movemask_epi8(result);
    if (mask == 0) return -1;
    return __builtin_ctz(mask); // index of first match
}
```

**Go (archsimd):**

```go
import (
    "math/bits"
    "simd/archsimd"
    "unsafe"
)

func findMatch(keys *[16]byte, search byte) int {
    // Load 16 bytes into a 128-bit register
    keyVec := archsimd.LoadInt8x16((*[16]int8)(unsafe.Pointer(keys)))
    // Fill all 16 lanes with the search byte
    cmpVec := archsimd.BroadcastInt8x16(int8(search))
    // Compare all 16 lanes at once
    mask := keyVec.Equal(cmpVec)
    // Extract one bit per lane into an integer
    bitmask := mask.ToBits()
    if bitmask == 0 {
        return -1
    }
    return bits.TrailingZeros16(bitmask) // index of first match
}
```

Same four steps, same machine instructions underneath. The Go version reads like normal code instead of requiring you to know that `_mm_loadu_si128` loads unaligned data or that `_mm_set1_epi8` broadcasts a byte. The compiler maps each call directly to the hardware instruction: `LoadInt8x16` -> `VMOVDQU`, `BroadcastInt8x16` -> `VPBROADCASTB`, `Equal` -> `VPCMPEQB`, `ToBits` -> `VPMOVMSKB`.

## What it feels like in practice

To test this beyond a micro-benchmark, I built [go-simd-art](https://github.com/hexfusion/go-simd-art), an Adaptive Radix Tree with SIMD-accelerated Node16 lookups. The original [2013 paper](https://db.in.tum.de/~leis/papers/ART.pdf) by Leis, Kemper, and Neumann describes a specific SIMD optimization: broadcast the search byte to all 16 lanes, compare at once, extract the match position from a bitmask. Existing Go ART implementations ([plar/go-adaptive-radix-tree](https://github.com/plar/go-adaptive-radix-tree), [arriqaaq/art](https://github.com/arriqaaq/art)) skip this because there was no clean way to express it.

With `archsimd`, the hot path in [node16.go](https://github.com/hexfusion/go-simd-art/blob/main/art/node/node16.go) is five lines:

```go
keys := archsimd.LoadInt8x16((*[16]int8)(unsafe.Pointer(&n.Keys)))
cmp := archsimd.BroadcastInt8x16(int8(c))
mask := keys.Equal(cmp)
bitmask := mask.ToBits()
bitmask &= (1 << n.Count) - 1
```

No assembly files. No `//go:noescape` pragmas. No build tags. The `unsafe.Pointer` cast from `*[16]byte` to `*[16]int8` is the one rough edge since `archsimd` works with signed types, but for equality comparison signedness does not matter.

On an AMD Ryzen AI 9 HX 370, the SIMD path is **15% faster** than scalar on dense trees (where Node16 dominates) and **14% faster** on random key workloads:

| Benchmark | Scalar | SIMD | Improvement |
|-----------|--------|------|-------------|
| SearchDense | 23.99 ns/op | 20.29 ns/op | -15.4% |
| Search (random) | 351.2 ns/op | 302.1 ns/op | -14.0% |
| Insert | 1479 ns/op | 1266 ns/op | -14.4% |

All operations are zero-allocation on the lookup path. Full benchmarks and source are in [go-simd-art](https://github.com/hexfusion/go-simd-art).

## Rough edges

**Signed types only.** `archsimd` provides `Int8x16` but not `Uint8x16`. Most data works with unsigned bytes. For equality this does not matter, but ordered comparisons (less-than) would need signedness handling. The [proposal discussion](https://github.com/golang/go/issues/73787) acknowledges this.

**GOEXPERIMENT everywhere.** Every `go build`, `go test`, `go run`, and CI invocation needs the flag. IDE tooling may not support it yet. This is the biggest practical barrier.

**AMD64 only.** The current implementation targets x86-64 SSE/AVX. ARM NEON support is mentioned in the proposal but not yet available.

## When does this matter?

15% on a hot inner loop is real for databases, routers, and parsers that do millions of lookups per second. For most application code, it does not. The value of `archsimd` is not that every Go program gets faster. It is that Go programs that currently drop to assembly for performance-critical paths can stay in pure Go.

The experiment flag means this is not production-ready yet, but the direction is clear. If you have a data structure with a tight loop over a fixed-size array, `archsimd` is worth trying now to see what the compiler produces.

*Parts of this post were written with assistance from [Claude](https://claude.ai).*
