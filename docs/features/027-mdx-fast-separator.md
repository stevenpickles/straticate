# [027] Real separator — fast vocals (MDX-family)

Branch: —
Status: **BLOCKED** — weights licence cannot be established
Dependencies: 026
PR: —

## Objective

A **fast** vocal separation tier. Feature 026's Mel-Band RoFormer runs at CPU
real-time factor 0.21–0.30 — 3.5–5× slower than real time — so a full song takes
minutes without a GPU. A fast tier is a product requirement, not a nicety.

## Why this is blocked

The intended model family was MDX-Net (UVR). **No party with standing has
stated the weights' licence.** Investigated 2026-08-24; every claim below was
re-verified against the primary source, not a search summary.

| Source | What it actually says |
| --- | --- |
| `Anjok07/ultimatevocalremovergui` issue **#2341** ("Question about bundling UVR models (Voc_FT) in a commercial app") | **Open, unanswered.** Opened 2026-08-10; its single comment is a *second* third party (`author_association: NONE`, 2026-08-22) asking the same question. |
| The repository README (§License, master) | Licenses **the code** as MIT, then asks third-party developers to "honor the MIT license by providing credit" — presupposing MIT extends to the models without granting it. |
| That README's `LICENSE` link | **404s** on `master` (`raw.../master/LICENSE`, and the GitHub licence API). A non-default branch carries stock MIT with no model clause. Known open bug #1798 (2025-04-03), never answered. |
| `TRvlvr/model_repo` (where UVR's own manifest points for MDX weights) | `license: null`. A *third* account, neither Anjok07 nor aufr33. |
| Hugging Face mirrors of the same bytes | `Politrees/UVR_resources` → `mit`; `seanghay/uvr_models` → none; `Eddycrack864/…` → `openrail`. A mirror cannot license what it does not own. |
| Every other licensing issue in the repo (#277, #1242, #1794, #1798, #2185, #2295) | One maintainer-level reply in total: aufr33, 2026-07-13 — *"I'll get back to you with an answer later."* No follow-up; that thread concerned a RoFormer model, not MDX-Net. |

**Second blocker:** even under the most permissive reading, the README asks for
credit to UVR and its developers. `grep -rn "attribution\|licensing" frontend/src`
returns nothing outside the generated types — the backend carries
`licensing.attribution` on `Model` and serves it, but **no frontend surface
renders it**. No maintainer has ever stated the required wording, so any string
would be invented.

Open-Unmix was checked as an alternative: **UMXL weights are CC BY-NC-SA 4.0**
(non-commercial). This is the norm — feature 026's Kim Vocal 2, relicensed to
MIT by its author on 2026-04-22, is the exception.

## What is already settled, for whenever this unblocks

The implementation design was worked out before the licence gate failed:

- **ONNX Runtime**, not torch. MDX-Net ships as ONNX, ORT is materially faster
  on CPU for this class, and `onnxruntime` 1.29.0 CPU is a **13.4 MiB** wheel
  plus `protobuf`/`flatbuffers` — trivial beside torch. It would also be the
  strongest demonstration that ARCHITECTURE.md §1 is real: a second inference
  **runtime**, not merely a second architecture, behind the same protocol.
- **The synthetic-fixture tier works.** A 4-input/4-output ONNX graph built with
  `onnx.helper` (Conv → Sigmoid → Mul, mask-shaped) is **285 bytes** and runs
  through an ORT `InferenceSession` with correct shapes and no warnings — the
  026-equivalent tiny-checkpoint tier, costing `onnx` in the dev group only.
  Note `torch.onnx.export` is **not** usable for that fixture: the legacy
  exporter raises a `DeprecationWarning` (fatal under the suite's `-W error`)
  and the dynamo exporter needs `onnxscript`, which is not installed.
- Plumbing shape: STFT front end, trim-context chunking, ORT session in a worker
  thread, `device=None` telemetry, with `pcm↔tensor` and run-state helpers
  factored out of `inference/roformer/`.

## Also required when this proceeds

Feature 032 gave `fake-standard-001` the `fast` tier to vacate `balanced`, but
`fake-vocals-001` still claims `balanced` in the `vocals` mode — it had nowhere
else to go, since `high_quality` is 026's and `fast` is this feature's. **027
must retier or drop `fake-vocals-001`**, or the catalog will fail to load.

## Options

1. **Ask upstream.** #2341 is open; two people have already asked and the
   maintainers have been silent on licensing since 2022. Not a plan with a date.
2. **Choose a differently-licensed fast model.** Meta's Demucs v4 weights have
   stated MIT terms; ZFTurbo's training repo publishes terms. Cost: probably not
   ONNX, weakening — not destroying — the second-runtime demonstration.
3. **Ship as an uninstalled catalog entry** the user installs themselves. This
   fixes nothing: Straticate still pins the URL and hash, and still cannot
   render the credit.
4. **Defer** until a model-management/attribution UI exists (feature 035 is the
   nearest claimed work).

## Acceptance criteria

- [ ] A weights licence stated by a party with standing to grant it
- [ ] Any required attribution renderable in-product
- [ ] Everything in 026's acceptance criteria, plus a CPU RTF measured against
      026's 0.21–0.30 — if the fast tier is not materially faster, that is a
      finding, not a failure
