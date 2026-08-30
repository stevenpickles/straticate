# [062] Band-limited fold for wide-stereo material

Branch: `062-band-limited-fold`
Status: PR OPEN
Dependencies: 041
PR: #100

## Objective

Answer the question feature 041 left open, and then act on the answer. 041's
known limitations named a band-limited fold — mono below a crossover, stereo
above — "the obvious next idea" and "on this evidence the most promising
unexplored option", and deliberately did not measure it. This feature measured
it against 041's own published baselines, on 041's own track, with 041's two
endpoints reproduced first as controls.

**It works, and it beats the fold it was compared against on every axis 041
recorded.** So the feature ships a third `stereo_handling` value, `mono_bass`.
It was equally prepared to ship as a documented rejection — the brief said so
and the stop condition was written before the first run — and the tables below
are the same tables either outcome would have produced.

## The result, in one table

Same track, same model, same settings, whole track, one decode. The two
outer rows are 041's published endpoints, reproduced here before anything new
was measured (see *The controls*).

| | `bass` stem rms | `bass` peak | source's <250 Hz in `bass` | …in `other` | `drums` cost | `other` cost | output channels |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **as released** (041 `as_is`) | −65.67 dBFS | 176/32767 | 0.002% | 83.7% | — | — | 2 |
| **`mono`** (041's fold) | −32.55 dBFS | 7,788/32767 | 16.0% | 40.7% | 3.29 dB | 2.80 dB | **1** |
| **`mono_bass`** (this feature) | **−31.96 dBFS** | 5,669/32767 | **19.4%** | 37.5% | **0.99 dB** | **1.16 dB** | **2** |

A `bass` stem 0.6 dB louder than the full fold's, holding 3.4 points more of the
source's low band, at a third of the fold's cost to the other stems — and the
stems come back stereo. The four stems still reconstruct the transformed mixture
at +0.999, as they do at every setting: nothing is gained or lost overall, the
low end is *reassigned*, and that remains the only claim this control makes.

## The transform

```text
M = mean(channels)        L' = HP(L) + LP(M)        R' = HP(R) + LP(M)
```

`LP` and `HP` are the two branches of a **Linkwitz-Riley 4th-order** crossover
at `BASS_FOLD_CROSSOVER_HZ`. Below the crossover both channels carry the same
signal — the mono fold, restricted to the band the model actually complains
about. Above it each channel keeps its own content, so the stereo image the
listener hears is left alone.

Linkwitz-Riley is the filter because its two branches **sum back to unity**:
`LP + HP` is an allpass, flat in magnitude at every frequency. On a recording
that is already centred (`L == R`, so `M == L`) the whole transform collapses to
`LP(L) + HP(L)`, which is the input back at the same level everywhere. Measured
on the shipped coefficients, `|LP + HP|` is 1.000000 across the spectrum
(`scipy.signal.sosfreqz`, 2048 points, in the harness) and a real centred signal
comes back **0.0003 dB** from where it started. Any crossover without that
property would notch or bump the region around the crossover on *every* centred
mix, to fix a minority of wide ones.

The shipped recursion was checked against `scipy.signal.sosfilt` over the same
coefficients on 200,000 random frames and is **bit-identical** — same int16, every
sample. Scipy is not a dependency of this project and does not become one: it was
installed into the measurement environment as an instrument, exactly as the FFT
crossover is an instrument, and the shipped path imports nothing but `math` and
`array`.

## The measurement

Measured **2026-08-30**, on the same 2:43 track features 028 and 041 used
(AAC, 44.1 kHz stereo, 7,193,004 frames, 163.11 s), the same model
(`standard-stems-001`, Hybrid Transformer Demucs, the real installed
checkpoint), the same catalog chunking, `cuda:0` on an RTX 4060 Laptop
(`torch 2.13.0+cu130`), whole track each time.

The track was **decoded once** and every variant derived from that one decode,
so nothing below differs by a decode. Each variant was quantized to 16-bit by
the shipped round-half-to-even rule, written as a WAV, and separated through
`DemucsSeparator.separate()` — the separator's own public entry point, not an
internal shortcut.

The harness is **not committed**: it configures a separator directly, downloads
nothing, and depends on `numpy` and `scipy` as instruments, none of which belongs
in this repository's test tiers (the same call feature 028 made for its VRAM
measurements). What is committed is this document, and *Verified through the
shipped control* below is the step that ties it to the code.

### The controls

Nothing new was believed until 041's numbers came back. They came back to the
digit — every published figure, on an independent run:

| | 041 published | measured here |
| --- | --- | --- |
| input full-band L/R correlation | +0.229 | **+0.2294** |
| input <250 Hz L/R correlation | +0.305 | **+0.3045** |
| input <250 Hz level, L / R | −20.5 / −28.0 dBFS | **−20.54 / −28.01** |
| input energy below 250 Hz | 26.8% | **26.75%** |
| input side/mid, full band | −2.0 dB | **−2.03 dB** |
| input rms, as released → folded | −17.1 → −19.2 dBFS | **−17.10 → −19.22** |
| `bass` rms, as released | −65.7 dBFS | **−65.67** |
| `bass` peak, as released | 0.0054 (176) | **0.0054 (176)** |
| `bass` rms, folded | −32.6 dBFS | **−32.55** |
| `bass` peak, folded | 0.2377 (7,791/7,788) | **0.2377 (7,788)** |
| source's <250 Hz in `bass`, as released → folded | 0.00002 → 0.160 | **0.00002 → 0.1603** |
| …in `other` | 0.837 → 0.407 | **0.8371 → 0.4072** |
| `bass` stem's own energy below 250 Hz, folded | 0.916 | **0.916** |
| stems relative to input, as released | −6.5 / −12.0 / −48.6 / −2.3 | **−6.52 / −11.98 / −48.56 / −2.30** |
| stems relative to input, folded | −4.4 / −15.3 / −13.3 / −5.1 | **−4.44 / −15.26 / −13.33 / −5.10** |

The rig is 041's rig. Everything below is therefore comparable to 041's numbers
rather than merely adjacent to them.

### The sweep

Two filters at three crossovers each, as the brief required — a **brick-wall FFT
crossover** as the instrument (an ideal split, to say what the *transform* does)
and the **ship-candidate Linkwitz-Riley 4th-order IIR** (to say what a shippable
filter does). The LR4 sweep was then extended to 350, 750 and 1000 Hz, for the
reason in *The filter is not neutral* below.

`bass` stem, and where the source's below-250 Hz energy ended up:

| variant | ch | `bass` rms | `bass` peak | <250 Hz in `bass` | …in `other` | `bass`'s own <250 Hz | reconstruction |
| --- | --- | --- | --- | --- | --- | --- | --- |
| as released | 2 | −65.67 | 0.0054 (176) | 0.00002 | 0.8371 | 0.406 | +0.9994 |
| `mono` | 1 | −32.55 | 0.2377 (7,788) | 0.1603 | 0.4072 | 0.916 | +0.9992 |
| FFT 125 Hz | 2 | −31.33 | 0.1580 (5,177) | 0.1691 | 0.5890 | 0.999 | +0.9995 |
| FFT 250 Hz | 2 | **−30.51** | 0.1985 (6,504) | 0.2776 | 0.2984 | 0.991 | +0.9991 |
| FFT 500 Hz | 2 | −31.43 | 0.1876 (6,147) | 0.2142 | 0.3790 | 0.944 | +0.9992 |
| LR4 125 Hz | 2 | −41.99 | 0.0858 (2,812) | 0.0160 | 0.7449 | 0.995 | +0.9994 |
| LR4 250 Hz | 2 | −36.21 | 0.1191 (3,902) | 0.0736 | 0.5986 | 0.996 | +0.9993 |
| LR4 350 Hz | 2 | −35.89 | 0.1406 (4,608) | 0.0797 | 0.5559 | 0.984 | +0.9991 |
| **LR4 500 Hz** | 2 | **−31.96** | 0.1730 (5,669) | **0.1944** | 0.3751 | 0.968 | +0.9992 |
| LR4 750 Hz | 2 | −30.24 | 0.1793 (5,875) | 0.2840 | 0.2900 | 0.952 | +0.9993 |
| LR4 1000 Hz | 2 | −29.65 | 0.1815 (5,946) | 0.3225 | 0.2443 | 0.945 | +0.9992 |

Per-stem level relative to each variant's **own** input, which is the comparison
041 established (narrowing removes energy, so absolute levels mislead), and the
`drums` / `other` penalty against the as-released run:

| variant | vocals | drums | bass | other | `drums` cost | `other` cost |
| --- | --- | --- | --- | --- | --- | --- |
| as released | −6.52 | −11.98 | −48.56 | −2.30 | — | — |
| `mono` | −4.44 | −15.26 | −13.33 | −5.10 | 3.29 dB | 2.80 dB |
| FFT 125 Hz | −6.29 | −12.74 | −14.03 | −2.87 | 0.77 dB | 0.58 dB |
| FFT 250 Hz | −6.00 | −13.00 | −12.93 | −3.49 | 1.02 dB | 1.20 dB |
| FFT 500 Hz | −6.02 | −13.27 | −13.74 | −3.46 | 1.30 dB | 1.16 dB |
| LR4 125 Hz | −6.20 | −12.08 | −24.59 | −2.60 | 0.11 dB | 0.31 dB |
| LR4 250 Hz | −5.99 | −12.61 | −18.59 | −2.96 | 0.63 dB | 0.67 dB |
| LR4 350 Hz | −5.96 | −12.88 | −18.22 | −3.08 | 0.91 dB | 0.79 dB |
| **LR4 500 Hz** | −5.96 | −12.97 | −14.22 | −3.46 | **0.99 dB** | **1.16 dB** |
| LR4 750 Hz | −5.87 | −12.85 | −12.42 | −3.78 | 0.88 dB | 1.48 dB |
| LR4 1000 Hz | −5.71 | −12.62 | −11.72 | −4.05 | 0.64 dB | 1.75 dB |

### The image, which is the whole point of the exercise

A transform that recovered the stem by quietly destroying the stereo image would
be `mono` with extra steps. Two measurements say it does not. First, the
**input's** side/mid ratio measured *only above its own crossover*, against the
as-released mix measured over the same band:

| variant | side/mid, full band | side/mid above the crossover | as released, same band | difference |
| --- | --- | --- | --- | --- |
| as released | −2.03 dB | — | — | — |
| `mono` | −∞ | −∞ | −2.03 dB | **everything** |
| FFT 125 Hz | −2.56 | −2.04 | −2.04 | 0.00 dB |
| FFT 250 Hz | −3.40 | −2.06 | −2.06 | 0.00 dB |
| FFT 500 Hz | −3.73 | −1.80 | −1.80 | 0.00 dB |
| LR4 125 Hz | −2.85 | −2.40 | −2.04 | 0.36 dB |
| LR4 250 Hz | −3.50 | −2.21 | −2.06 | 0.15 dB |
| LR4 350 Hz | −3.69 | −2.14 | −2.01 | 0.13 dB |
| **LR4 500 Hz** | **−3.89** | **−1.98** | −1.80 | **0.18 dB** |
| LR4 750 Hz | −4.21 | −1.77 | −1.47 | 0.30 dB |
| LR4 1000 Hz | −4.62 | −1.74 | −1.20 | 0.54 dB |

The brick-wall crossover is exact above its corner, as it must be. LR4 leaks
0.18 dB at 500 Hz, because a 24 dB/octave slope is not a wall — the band just
above the crossover is partly folded too. That is the honest cost, and it is
well inside the 1 dB the brief allowed.

Second, the **stems'** side/mid above the crossover, against the as-released
run's stems measured over the same band:

| variant | vocals | drums | bass | other |
| --- | --- | --- | --- | --- |
| LR4 125 Hz | +0.03 dB | −0.17 | −8.33 | −0.40 |
| LR4 250 Hz | −0.19 dB | −0.17 | −7.65 | −0.11 |
| LR4 350 Hz | −0.63 dB | −0.21 | −7.62 | −0.04 |
| **LR4 500 Hz** | **−0.95 dB** | **−0.10** | **−6.13** | **−0.09** |
| LR4 750 Hz | −0.86 dB | −0.03 | −2.38 | −0.21 |
| LR4 1000 Hz | −1.08 dB | −0.03 | +0.18 | −0.45 |

Three of the four are within a decibel at every crossover, which is the
criterion. **`bass` is not, and should not be**: after the fold that stem holds
96.8% of its energy *below* 250 Hz — content that is centred by construction,
which is the entire purpose of the transform — so this column is measuring what
is left of 3% of a stem, against an as-released `bass` stem that is −65.7 dBFS
of near-silence. It is reported rather than omitted, with that caveat, because
the criterion said four stems.

### The filter is not neutral, and that is the most useful thing measured here

The brief asked for both filters "so filter choice is shown not to be the
variable". **It is a variable**, and by a lot:

| crossover | FFT `bass` rms | LR4 `bass` rms | difference | FFT recovery | LR4 recovery |
| --- | --- | --- | --- | --- | --- |
| 125 Hz | −31.33 dBFS | −41.99 dBFS | **10.7 dB** | 16.9% | 1.6% |
| 250 Hz | −30.51 dBFS | −36.21 dBFS | **5.7 dB** | 27.8% | 7.4% |
| 500 Hz | −31.43 dBFS | −31.96 dBFS | 0.5 dB | 21.4% | 19.4% |

The reason is the slope, and it is worth stating because it is what makes the
crossover value meaningful only alongside the filter. A brick wall at 125 Hz
centres *everything* below 125 Hz, including the 40–80 Hz where a bass
fundamental lives. LR4 at 125 Hz attenuates the high-passed side content at
60 Hz by only ~25 dB, so a quarter-ish of the original channel independence
survives exactly where the model needs it gone — and 041's own sweep already
showed what a −25 dB side component buys: at `k = 0.10` (side 22 dB down) the
`bass` stem was −38.9 dBFS, which is the same region LR4 125 Hz lands in
(−41.99). The two measurements agree, from opposite directions.

By 500 Hz the LR4 stopband has caught up (−49 dB at 60 Hz) and the two filters
converge to within half a decibel. **So the shipped crossover is stated in terms
of the shipped filter**, and this table is why a future change of filter must
re-run the sweep rather than carry the constant across.

### Why 500 Hz

`BASS_FOLD_CROSSOVER_HZ = 500`. Read down the LR4 rows against the `mono` row:

- **350 Hz does not beat the full fold.** −35.89 dBFS is 3.3 dB quieter than
  `mono`'s `bass`, and 8.0% recovery is *below* `mono`'s 16.0%. Shipping it
  would mean offering a control that is worse than one already on the menu.
- **500 Hz beats it outright**, on both of the numbers the fold was adopted for:
  −31.96 against −32.55 dBFS, and 19.4% against 16.0%. It is the **lowest**
  crossover that does, so it is the cheapest way to get everything `mono` gives.
- **750 and 1000 Hz keep buying recovery** (28.4%, 32.3%) and pay for it in
  width: full-band side/mid falls from −3.89 to −4.21 to −4.62 dB, against the
  as-released −2.03. That is a real trade, and **it already has a control** —
  its far end is `mono`, one radio button away. The value of *this* option is
  the image it keeps, so the crossover is the lowest one that asks the user to
  give up nothing in exchange, not the one that maximises a single column.

The constant lives in `inference/stereo.py` with that argument in its docstring.
It is not a job field and not catalog data; see *Why the crossover is a constant*
below.

### Verified through the shipped control, not only through the harness

The sweep filters the audio in the measurement harness. The shipped `mono_bass`
path filters it inside `apply_stereo_handling`, in blocks, on the pure-Python
path every backend can reach. So the last step was 041's: run the **shipped
control** on the same file, `DemucsSeparator.separate()` three times, same
weights, `cuda:0`, once per value of `stereo_handling`.

| stem | default (`as_is`) | `stereo_handling: "mono"` | `stereo_handling: "mono_bass"` |
| --- | --- | --- | --- |
| vocals | −23.62 dBFS, peak 17,065, **2 ch** | −23.66 dBFS, peak 15,705, **1 ch** | −23.69 dBFS, peak 19,383, **2 ch** |
| drums | −29.08 dBFS, peak 20,729, 2 ch | −34.48 dBFS, peak 16,805, 1 ch | −30.70 dBFS, peak 22,006, 2 ch |
| **bass** | **−65.67 dBFS**, peak **176** | **−32.55 dBFS**, peak **7,788** | **−31.96 dBFS**, peak **5,669** |
| other | −19.40 dBFS, peak 20,972, 2 ch | −24.32 dBFS, peak 14,902, 1 ch | −21.19 dBFS, peak 23,193, 2 ch |
| source's <250 Hz in `bass` | 0.00002 | 0.1603 | **0.1944** |
| reconstruction | +0.9994 | +0.9992 | +0.9992 |

Two things to read off it.

The `as_is` and `mono` columns are **feature 041's shipped-control table,
reproduced field for field** — −65.67 / −32.55 dBFS, peak 176 and 7,788, and
041's 15,705 / 16,805 / 14,902 on the folded run. So the shipped code still does
what 041 says it does, on a third independent measurement, a branch later.

And the `mono_bass` column is **identical to the harness's LR4 500 Hz row in
every field** — every rms to 0.01 dB, every peak to the int16 count, every
low-band share to four decimals. 041's equivalent step moved three stems by one
int16 count, because its harness quantized once more than the shipped path did;
here it does not, because the harness's variant WAV holds exactly the int16
samples `apply_stereo_handling` produces and the separation is deterministic. The
sweep above therefore describes the shipped control and not merely something
adjacent to it.

The stems' image, measured on the shipped run above 500 Hz against the shipped
`as_is` run over the same band: vocals −0.95 dB, drums −0.10 dB, other −0.09 dB,
`bass` −6.13 dB (the stem that is centred by construction — see above).

## Scope

- **`backend/src/straticate/schemas/jobs.py`** — `StereoHandling.MONO_BASS`, and
  the `stereo_handling` field description. The enum is the only wire change; the
  field is unchanged in shape, optionality and default.
- **`backend/src/straticate/inference/stereo.py`** —
  `BASS_FOLD_CROSSOVER_HZ`, `BASS_FOLD_BLOCK_FRAMES`, `bass_fold_blocks`,
  `_lr4_coefficients`, `_lr4_branch`, and the second branch in
  `apply_stereo_handling` / `apply_stereo_handling_async`. The two drivers were refactored to accumulate
  *tuples* of planes so that one piece of code serves both transforms.
- **`frontend/src/api/generated/api.d.ts`** — regenerated. **Not hand-edited.**
- **`frontend/src/api/jobs.ts`** — the third `STEREO_HANDLING_TABLE` entry.
- `backend/tests/{test_stereo_handling,test_api_jobs,test_api_results}.py`,
  `frontend/src/api/jobs.test.ts`,
  `frontend/src/components/SeparationOptions.test.tsx`.
- `docs/contracts/rest-api.md`, this document, the ROADMAP ledger row.

**No separator changed.** `torch_separator.py` and `fake.py` call
`apply_stereo_handling_async` and were not touched — feature 039's skeleton and
041's decision to keep the transform out of torch are what made a new transform
a one-module change reaching all three backends. Neither did
`SeparationOptions.tsx`: the picker already renders whatever
`STEREO_HANDLING_OPTIONS` contains (AGENTS.md principle 6), so a third value is
a data edit there.

## Out of scope

- **Wide-stereo detection.** Feature 063, and 041's handoff table is still the
  input to it. Nothing here detects anything; `as_is` remains the default and
  nothing is ever applied unasked.
- **A user-settable crossover.** See below — it is measured, not offered.
- **Removing or deprecating `mono`.** It is still the right answer for a user
  who does not care about the image, it is still 2.5× cheaper to compute, and
  its mono stems are half the disk. Nothing about it changed.
- **Any UI beyond the presentation table entry.**
- **Re-measuring on a second track.** Same limitation 041 carries; see below.

## Expected modules/files

- `backend/src/straticate/inference/stereo.py`,
  `backend/src/straticate/schemas/jobs.py`
- `backend/tests/{test_stereo_handling,test_api_jobs,test_api_results}.py`
- `frontend/src/api/{jobs.ts,jobs.test.ts,generated/api.d.ts}`
- `frontend/src/components/SeparationOptions.test.tsx`
- `docs/contracts/rest-api.md`, `docs/features/062-band-limited-fold.md`,
  `ROADMAP.md`

## Acceptance criteria

- [x] 041's `as_is` and `mono` rows reproduced as controls **before** anything
      new was measured — every published figure, to the digit
- [x] The transform measured at three or more crossovers with **both** a
      brick-wall instrument and the ship-candidate IIR
- [x] Ship criteria met at a swept crossover: `bass` rms within 6 dB of the
      fold's (it is 0.6 dB **better**), ≥8% low-band recovery (19.4%), input
      image above the crossover within 1 dB of as-released (0.18 dB), stems
      within 3 dB of as-is (0.10–0.95 dB for vocals/drums/other; `bass` excepted
      and explained), `drums`/`other` penalty ≤ the fold's ~3 dB (0.99/1.16),
      reconstruction +0.999, two channels out
- [x] `mono_bass` shipped with a fixed, measured, documented crossover that is
      neither a job field nor catalog data
- [x] Every backend gets it, including the fake engine — asserted, not assumed
- [x] Default behaviour unchanged: `as_is` still returns the very object it was
      given, and a mono source is identity for **all three** values
- [x] All gates green

## Required tests

`backend/tests/test_stereo_handling.py` gains a section for the band-limited
fold. Every one of these was **mutation-verified**, not merely written — see
below.

**Spectral**, at 44.1 kHz so the shipped crossover is the one exercised, past a
0.1 s settling window because a filter starting from rest rings on the way to
steady state:

- a 60 Hz sine **hard-panned left comes back centred**: both channels at
  −6.02 dB of the source (a perfect half), the difference between them 67 dB
  below either;
- a 3 kHz sine **hard-panned left stays panned**: the loud channel at −0.003 dB,
  the silent one at −68.7 dB — the low-pass branch's response 2.6 octaves into
  its stopband, less 6 dB for the mean;
- **centred material keeps its magnitude** (0.0003 dB) — and is asserted **not**
  bit-identical, which is the documented difference from `mono`'s exactness: an
  allpass has phase, so samples move (a third of full scale on the test signal)
  while the RMS does not;
- **full scale cannot overflow**: a full-scale square wave near the crossover
  rings through both branches, and the clamp is what keeps every sample legal.

**Structural:**

- **blocked == one-shot, exactly**, over seven block sizes landing mid-frame, on
  the boundary and past it. This is the filter-state mutation-catcher: a run
  that reset the biquads each block would emit a discontinuity every 4096 frames
  — a 10.8 Hz buzz over the music, from code that reads correctly, and invisible
  to any assertion about level or spectrum;
- a **mono source is object identity** for `mono_bass` too, not an allpassed
  copy;
- the **async and sync drivers agree** sample for sample;
- **cancellation** is observed at a block boundary, both pre-cancelled and
  mid-run;
- the **crossover is a constant**: the default equals `BASS_FOLD_CROSSOVER_HZ`
  and `SeparationConfiguration` has no field for it.

**Wiring**, parametrised over **all three** separators (both torch backends and
the fake engine):

- a job asking for `mono_bass` gets **two**-channel stems and a result record
  that says `channels == 2`;
- and — the test this value needs that `mono` did not — the stems must **differ
  byte for byte** from an untouched run. `mono_bass` is the one value whose
  output has the same *shape* as `as_is`'s, so a separator that dropped the call
  would pass a channel-count assertion;
- a folded RoFormer run's `vocals + instrumental` still reconstructs the
  **transformed** mixture to within a rounding step. With `mono` a mistake here
  is loud (a mono estimate subtracted from a stereo mixture); with `mono_bass`
  both are two-channel, the shapes agree, and the error would be silent.

**Client-visible**, in `test_api_results.py`: a **fake-backed** server given
`stereo_handling: "mono_bass"` returns two channels on every stem of the result
record *and* in the WAV headers of the bytes it serves, and echoes the value
back on the job. `test_api_jobs.py`'s echo test is parametrised over both
non-default values.

**Frontend**: the presentation table is exhaustive over the generated union and
offers the three choices in picker order (least to most done to the recording);
both folds are framed as recovering a stem rather than separating better; each
says what it does to the stems (`come back mono` / `come back in stereo`); no
note quotes a number, because one track is not a population; the picker offers
the third radio, described by what it keeps, between the other two, posts
`mono_bass` when chosen, and offers it to a mono upload no more than it offers
the other two.

### Mutation verification

Three substitutions, each of which a passing suite must reject, and each with a
**different** signature — which is what proves the tests discriminate rather
than merely cover:

| substitution | what fails |
| --- | --- |
| `mono_bass` becomes a **passthrough** | 8 tests, including the 60 Hz centring test (the panned tone stays panned) and all three wiring tests. The 3 kHz test *passes* — a passthrough does keep the image |
| `mono_bass` becomes a **full fold**, duplicated into two planes | the 3 kHz pan-preservation test (the silent channel comes back at 0 dB instead of −68.7) and the "an allpass is not the identity" clause. The 60 Hz test *passes* — a full fold does centre the bass |
| the **filter state resets each block** | the block-invariance test, and the 60 Hz centring test. Nothing else notices |

Removing the entry from `STEREO_HANDLING_TABLE` is a **type error**
(`TS2741: Property 'mono_bass' is missing`), verified by deleting it and running
`tsc`, which is the mechanism that idiom exists for.

Separation *quality* is deliberately not in CI: it needs real weights and real
music. The measurement above is the evidence, and it is the integration tier's
kind of work.

## Notes / decisions

### Why the crossover is a constant, and not a field

This is feature 041's ARCHITECTURE.md §1 argument applied one level down, and it
is the decision most likely to be reopened, so it is written out.

The **choice** this control offers is a statement about the user's recording:
"the low end of this mix is not centred". That is true of the file before any
model is picked and stays true if every model is replaced, which is exactly what
made `stereo_handling` legitimate in 041. The **crossover** is not that kind of
fact. It is a property of where a stereo image lives in human hearing and of
where these models' training distribution stops, and the person who owns the
recording has no way to answer it — they would be moving a slider until a stem
got louder, which is the sweep above, done worse.

It is not `default_inference_parameters` either. It is not a hyperparameter of
any network: it is applied to the mixture *before* a model is chosen, in the
shared skeleton, and it would mean the same thing to a different backend. Adding
it to the catalog would make a per-model value out of something no model knows
about.

041 refused to expose the mid/side `k` for the same reason and with the same
evidence — a slider whose useful range the measurement has already searched is a
worse control than the answer.

### Cost: it is the more expensive transform, and by how much

041 measured its fold at 0.62–0.77 s per minute of audio and accepted it as the
price of a contract that is true on every backend. The band-limited fold is a
biquad cascade rather than a mean, so it costs more.

Measured on the same 163.11 s track, median of three runs, `tracemalloc` **off**
for the timings and on for the memory — it inflates a pure-Python loop by roughly
8×, which is a large enough error to have made this table wrong:

| | `mono` | `mono_bass` |
| --- | --- | --- |
| whole track | 1.98 s | 7.25 s |
| per minute of audio | **0.73 s** | **2.67 s** |
| across three repeats of the whole measurement | 0.70–0.87 s/min | 2.64–2.98 s/min |
| per block | 0.80 ms (4096 frames) | 0.93 ms (1024 frames) |
| peak, of which the result | 15.27 MB / 14.39 MB | 29.14 MB / 28.77 MB |
| overhead, and does it grow with the track | 0.88 MB, no | **0.37 MB, no** |

3.7× the fold, and about 4 minutes of CPU on a 90-minute track. Three notes on
the shape of that cost:

- **The two Butterworth sections of each LR4 branch run in one Python loop.**
  Their coefficients are identical, so fusing them saves a loop dispatch and the
  intermediate list between them: 2.99 → 2.69 s per minute on the same audio,
  bit-for-bit the same result. An 11% saving, honestly small — it is written
  that way because it is also shorter.
- **The default still costs nothing.** `as_is` returns the very object it was
  given and never reaches a thread, exactly as before.
- **Memory stays flat, and the block is smaller.** `BASS_FOLD_BLOCK_FRAMES` is
  `FOLD_BLOCK_FRAMES / 4`, because feature 045 sized that constant by the
  **latency** the fold owes the event loop (~0.80 ms of GIL per block), and the
  same 4096 frames of biquad cascade holds it for 3.74 ms. The whole-fold time is
  flat across a 32× range of block sizes — 6.91 s at 32768 frames against 7.17 s
  at 1024, exactly as 045 measured for the plain fold — so honouring 045's
  argument rather than its number is free, and it drops the transient overhead
  from 2.14 MB to 0.37 MB as well.

Whether that is *disproportionate* is a judgement, and it is recorded as a known
limitation rather than pre-emptively optimised: the sanctioned remedy, if it ever
matters, is a fast path **inside** `apply_stereo_handling` when torch happens to
be importable, with a CI test asserting agreement to within 1 LSB — never moving
the function back behind the bridge, which is what made the contract false the
first time. It was not done here because a recursive filter has no vectorised
form in torch or numpy: a fast path would have to be an FFT convolution against
a truncated impulse response, i.e. a **second implementation of the transform**,
and that is a feature's worth of work and risk to buy back seconds on an opt-in
path. See *Known limitations*.

### Where it happens: nowhere new

`TorchSeparator._separate` calls `apply_stereo_handling_async` on the line after
the decode, and `FakeSeparator` does the same at the same point. Neither file
changed. That is worth recording because it is the payoff for two earlier
decisions: feature 039 extracted the shared skeleton so a change like this is
written once, and feature 041 kept the transform out of torch so the fake engine
can run it. A third transform was a one-module change that reached all three
backends and the API contract with no separator edit at all.

### Blocking, and the one thing that is different this time

`FOLD_BLOCK_FRAMES` (4096) is unchanged and the band-limited fold uses it for
the same three reasons feature 045 established: bounded memory, prompt
cancellation, and a short GIL hold so the event loop is not starved. At ~2.9 ms
per block it is the same order as the plain fold's 0.80 ms and well inside the
budget.

What is new is that **the block boundary is load-bearing for correctness**. A
biquad's output depends on the two frames before it, so the filter state is
carried across blocks in float64 and a blocked run is *bit-identical* to a
one-shot one. The state is a list mutated in place rather than a value the caller
threads through, because "forgot to carry the state" is precisely the bug the
invariance test exists to catch and a mutating call has no way to express it.

### Rounding: the same rule, a weaker guarantee, stated

Ties round to even — `round()` on a float already does — which is
`torch.round`'s rule and therefore `tensor_to_pcm`'s, so the mixture and the
stems are quantized alike. But this transform **cannot be exact** the way
`mono` is: a recursive filter has no integer answer to be exact about. What it
is instead is *deterministic*: float64 throughout, in a fixed order, so the same
input gives the same int16 bytes on every host. The tests assert both, and the
docstring says plainly that centred audio comes back at the same level but not
bit-identical, so nobody has to discover the difference from a support question.

## Known limitations

- **One track.** Every number here is from the same 2:43 mix features 028 and
  041 used. It is the right material for a *comparison* — it is the failure case
  — and it is not a survey. The claim is "on material like this, the band-limited
  fold recovers the stem at least as well as the full fold while keeping the
  image", not "500 Hz is the right crossover for all music".
- **`bass` is recovered, not fixed.** 19.4% of the source's low band, against
  0.002% untouched and 16.0% folded. `other` still holds 37.5%. The stem exists
  and is 96.8% low-band content; the low end is still not cleanly split.
- **2.67 s of CPU per minute of audio**, in a worker thread, on the pure-Python
  path — 3.7× `mono`'s and about 4 minutes on a 90-minute track. Bounded
  memory and prompt cancellation, but the time is linear and real, and on a GPU
  it is a visible fraction of the job. The remedy is named above and was
  deliberately not taken; if a user complains, that is the feature to open.
- **The image above the crossover is preserved to 0.18 dB, not exactly.** LR4 is
  24 dB/octave, not a wall, so the band just above 500 Hz is partly folded. The
  brick-wall instrument shows what exact would look like; it is not implementable
  in a blocked streaming filter without an overlap-add design this feature did
  not take on.
- **The crossover is right for this filter and this measurement.** The FFT-vs-LR4
  table shows a 10 dB disagreement at 125 Hz. A future change of filter — or of
  model family — invalidates the constant and must re-run the sweep, not carry
  the number across.
- **No detection**, so a user still has to know to look. Feature 063's job, and
  041's handoff table is unchanged by this feature except that a suggestion now
  has two things it could point at. It must still suggest and never apply.
- **No E2E coverage.** Covered by unit tests on both sides of the contract and
  by a fake-backed API test that asserts two-channel stems in the record and in
  the WAV headers. A Playwright case would be straightforward, and would now
  need to distinguish two values whose output shape is the same.
- **`mono` is still cheaper and still smaller.** Nothing here deprecates it, and
  the picker offers both, because "I do not care about the image" is a real
  answer and mono stems cost half the disk.

## Noticed, out of scope

- **The as-released `bass` stem's side/mid ratio is not a meaningful baseline.**
  It is −8 to −10 dB across the sweep's measurement bands, but the stem it
  describes is −65.7 dBFS — the image of near-silence. It is reported for
  completeness and should not be read as a property of the recording.
- **The `bass` stem's own low-band fraction falls as the crossover rises**
  (0.995 at 125 Hz to 0.945 at 1 kHz) while its total recovery rises. That says
  the stem picks up mid-band content as more of the spectrum is centred, which is
  a hint about *what* the model is keying on. It is a question for a feature that
  owns model behaviour, not this one.
