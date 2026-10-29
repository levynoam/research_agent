---
# arXiv categories to scan every day. Full list: https://arxiv.org/category_taxonomy
categories:
  - cs.LG
  - cs.CL
  - cs.AR
  - cs.DC
---

# Research criteria

The agent reads this whole file before every run. Write it the way you would brief
a sharp research assistant: plain language is fine, and examples help a lot.

Each topic is a `##` heading under "Topics". The heading text is the topic name used
in the digest, so keep it short.

> These starter topics are placeholders. Replace them with your own.

# Topics

## LLM inference efficiency
Methods that make large-model inference faster or cheaper at serving time.

- Include: speculative decoding, KV-cache compression or eviction, quantization of
  weights/activations/KV cache, efficient attention kernels, batching and scheduling
  in serving systems, early exit, mixture-of-experts serving.
- Exclude: training-only efficiency (e.g. faster pre-training) unless it also changes
  inference; pure application papers that just use an existing fast-inference library.
- Example of a strong match: a new speculative decoding scheme with measured
  end-to-end speedups on a real serving stack.

## Hardware-aware ML / accelerator architecture
Work that connects ML algorithms to the hardware they run on.

- Include: GPU/accelerator microarchitecture for ML, hardware-software co-design,
  performance modeling and simulation of ML workloads, memory-hierarchy-aware kernels,
  low-precision number formats (FP8, FP4, MX formats).
- Exclude: general-purpose CPU architecture with no ML angle; FPGA papers that are
  only about tooling.

# General guidance

- Favor papers with real measurements over purely theoretical claims.
- A survey is only interesting if it covers one of the topics above in depth.
- When unsure whether a paper fits, lean toward including it at triage; the full
  read will filter it out if it doesn't hold up.
