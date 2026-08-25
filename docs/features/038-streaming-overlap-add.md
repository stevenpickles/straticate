# [038] Streaming overlap-add (bounded VRAM)

Branch: `038-streaming-overlap-add`
Status: PLANNED
Dependencies: 026

## Objective

Make peak VRAM a function of the **chunk size**, not of the track length, so a
long enough track cannot exhaust the card.

## The problem, measured

Feature 036 measured `vocals-hq-001` on an RTX 4060 (8188 MiB), one fresh
process per configuration, whole-device usage sampled every 20 ms:

| clip | peak allocated (MiB) | whole-device peak (MiB) |
| --- | --- | --- |
| 10 s | 1,549 | 3,001 |
| 30 s | 1,575 | 3,023 |
| 2 min | 1,700 | 3,155 |
| 6 min | 2,021 | 3,475 |
| 10 min | 2,343 | **4,213** |

Peak **grows linearly with track length** at roughly **1.35 MiB per second of
audio**, fitting (with a chunk-size sweep) to within 3 MiB:

```text
peak ≈ 895 + 1.82 × (chunk_size / 1000) + 1.35 × seconds
```

Chunking bounds the *working* set but not the total, because feature 026 holds
the decoded mixture, the output accumulator and the per-chunk weight tensor on
the **device** for the whole track — 026 records this as a known limitation
("streaming overlap-add is future work"); 036 gave it numbers.

**Concrete thresholds:** a 4 GiB card exhausts at roughly **9 minutes** of
audio, an 8 GiB card at roughly **20 minutes**. Long DJ sets, live recordings
and album-length files are inside that range, and the failure is a CUDA OOM
part-way through a job the user has already waited minutes for.

Note also that `max_memory_allocated` counts tensors only: the CUDA context is
**1,079 MiB before a single tensor**, and the allocator reserves 330–750 MiB
above live tensors. What a card must have free is roughly twice the reported
figure — which is why 036 set `recommended_vram_mb: 6144` and
`minimum_vram_mb: 4096` rather than the ~1.6 GiB a naive reading suggests.

## Scope

- Stream the overlap-add: keep only the chunks in flight on the device, and
  accumulate output on the host (or to disk) so device residency is bounded by
  `chunk_size × num_overlap`, not by duration.
- Keep the existing `Separator` contract exactly: real chunk-grained progress,
  cooperative cancellation between chunks, `.part`-then-rename stem writes,
  and `runtime_stats()` as a cheap non-blocking snapshot.
- Re-measure afterwards and **correct `requirements` again** if the bound
  genuinely changes — the figures above are the baseline to beat.
- Consider whether the decoded mixture needs to be resident at all, or can be
  read in chunk-sized windows from the decoded PCM on disk.

## Out of scope

Changing separation output (a streaming implementation must be
**bit-identical** to today's for the same input — prove it, the way feature 026
proved its vendored mel filters bit-identical to librosa). Any other
architecture. Any quality or speed tuning: `num_overlap` is a quality/speed
dial that 036 measured as costing throughput while buying no memory, and that
is a separate question.

## Acceptance criteria

- [ ] Peak device memory is flat (within noise) across 30 s, 2 min, 6 min and
      10 min inputs
- [ ] Output is bit-identical to the pre-change implementation for the same
      input and parameters
- [ ] Progress, cancellation and telemetry behave exactly as before
- [ ] `requirements` re-measured and corrected if warranted
- [ ] A track long enough to exhaust the card today completes

## Required tests

A memory-flatness test is hardware-dependent and belongs in the
`integration`/`gpu` tier, not normal CI. In normal CI, assert the *structural*
property that makes flatness possible — that device residency does not scale
with the number of chunks — against the synthetic checkpoint.
