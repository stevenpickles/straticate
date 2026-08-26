# [041] Mono fold-down for wide-stereo material

Branch: `041-mono-folddown-option`
Status: PR OPEN
Dependencies: 028
PR: #57

## Objective

Let a user separate wide-separation stereo material without losing a stem.

## The evidence

Feature 028's known limitations record the full investigation. In short, on a
1968 stereo mix Demucs produced an effectively silent `bass` stem
(−66.2 dBFS, peak 176/32767) while the other three were healthy — because that
mix has near-independent channels (**full-band L/R correlation +0.23**, against
0.7–0.95 for modern productions) and a low end hard-panned left (−21.2 dBFS L
against −27.0 dBFS R). Demucs is trained on MUSDB18, where bass is essentially
always centred.

**Folding the input to mono recovers it**, verified on the same track, same
model, same settings:

| | stereo (as released) | mono fold-down |
| --- | --- | --- |
| bass rms | −65.7 dBFS | **−32.6 dBFS** |
| bass peak | 0.0054 | **0.2377** |

33 dB. `htdemucs_ft` was tested and does **not** help (its bass specialist gives
−66.2 dBFS), so this is the change that works.

028 added "with the other three stems unaffected", and this feature's own
measurement makes that claim, and the word *fixes*, weaker than they look — see
*Two things worth being honest about* below. 028's known-limitations section now
carries a pointer to the same correction.

## The question this feature had to answer

Mono fold-down was proven but not obviously the *right* control to expose. The
brief named three candidates and required the second to be **measured** rather
than reasoned about, because if a partial narrowing worked it would be strictly
better than throwing away the stereo image.

It was measured. **It does not work.** See *The measurement* below. The control
this feature ships is therefore the fold-down — a two-value
`stereo_handling` on `SeparationConfiguration` (`as_is` | `mono`) — and
detection-and-suggestion is deliberately not implemented (see *Out of scope*).

## The measurement

Measured 2026-08-25 on the same track feature 028 used (2:43, AAC, 44.1 kHz
stereo; full-band L/R correlation **+0.229**, <250 Hz correlation +0.305, low
band −20.5 dBFS L against −28.0 dBFS R, 26.8% of the source's energy below
250 Hz; the sub-250 Hz figures differ in the last digit from 028's +0.32 /
−21.2 / −27.0 / 25.1% because they are measured here with a brick-wall FFT
low-pass — the full-band correlation, which is the load-bearing number, agrees
exactly). Same model (`standard-stems-001`, Hybrid Transformer Demucs, real
installed weights), same catalog chunking, `cuda:0` on an RTX 4060 Laptop, whole
track each time. The audio was preprocessed with

```text
M = (L + R) / 2      S = (L - R) / 2
L' = M + k·S         R' = M - k·S
```

so **k = 1 is the untouched mix and k = 0 is the mono fold-down** — the two ends
of the same one-parameter family, which is what makes them comparable at all.
The track was decoded once to 16-bit PCM and every variant derived from that one
decode, so nothing below differs by a decode.

### Stem level, in dBFS

| k | input L/R corr. | side/mid | vocals | drums | **bass** | other | bass peak | bass peak (of 32767) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1.00 (as released) | +0.229 | −2.0 dB | −23.6 | −29.1 | **−65.7** | −19.4 | 0.0054 | 176 |
| 0.75 | +0.479 | −4.5 dB | −23.7 | −29.9 | **−65.7** | −20.6 | 0.0116 | 380 |
| 0.50 | +0.729 | −8.0 dB | −23.7 | −30.7 | **−65.2** | −21.6 | 0.0188 | 616 |
| 0.35 | +0.858 | −11.1 dB | −23.8 | −31.1 | **−65.4** | −22.1 | 0.0175 | 573 |
| 0.25 | +0.925 | −14.1 dB | −23.8 | −31.3 | **−52.6** | −22.5 | 0.0417 | 1,368 |
| 0.10 | +0.988 | −22.0 dB | −23.8 | −31.9 | **−38.9** | −23.2 | 0.1109 | 3,634 |
| **0.00 (mono)** | +1.000 | −∞ | −23.7 | −34.5 | **−32.6** | −24.3 | **0.2378** | **7,791** |

The `k = 1.00` and `k = 0.00` rows reproduce feature 028's −65.7 / −32.6 and
0.0054 / 0.2377 to the digit, on an independent run through the separator's own
`separate()` entry point. That is the control that makes the seven rows between
them worth reading — and *Verified through the shipped control* below repeats
both endpoints a third time, through the option this PR adds.

### Where the source's low band actually goes

Level alone can mislead — a stem can get louder without getting *righter* — so
the sharper question is what fraction of the source's **below-250 Hz energy**
each stem ends up holding. Same runs, measured by brick-wall FFT low-pass:

| k | share of source's <250 Hz energy in `bass` | …in `other` | `bass` stem's own energy that is <250 Hz |
| --- | --- | --- | --- |
| 1.00 | 0.00002 | 0.837 | 0.406 |
| 0.75 | 0.00003 | 0.818 | 0.432 |
| 0.50 | 0.00004 | 0.798 | 0.518 |
| 0.35 | 0.00004 | 0.782 | 0.491 |
| 0.25 | 0.0016 | 0.762 | 0.961 |
| 0.10 | 0.0399 | 0.608 | 0.982 |
| **0.00** | **0.160** | **0.407** | 0.916 |

### What the numbers say

1. **Partial narrowing buys nothing until the stereo image is already gone.**
   At `k = 0.35` the input's L/R correlation is **+0.858** — inside the 0.7–0.95
   band feature 028 measured for modern productions, i.e. the mix now *looks*
   like what the model was trained on — and the bass stem is still −65.4 dBFS
   holding 0.004% of the low band. The recovery is a late cliff, not a gradient.
2. **The first meaningful recovery is at `k = 0.10`**, where the side component
   is 22 dB below the mid and the L/R correlation is +0.988. For a listener
   that is mono with a rounding error, and it is still 6.3 dB short of the
   fold-down. The output stems say the same thing: at `k = 0.10` their own
   side/mid ratios are −20 to −34 dB.
3. **So the premise fails.** There is no `k` that both recovers the stem and
   leaves an audible stereo image. "Keep some width and get the bass back" is
   not a trade this material offers, so there is no partial setting worth
   exposing — and a slider whose useful range is `[0, 0.1]` would be a worse
   control than a switch, not a better one.
4. **Fold-to-mono is therefore the control**, and it is the simpler one to
   explain, which is the tie-breaker the measurement earned.

### Two things worth being honest about

**The other stems are not untouched.** 028 said "the other three stems
unaffected", which is true of *level* and, at the resolution that table had,
true enough. Measured relative to each variant's own input level (removing the
confound that narrowing itself removes energy — the input drops from −17.1 to
−19.2 dBFS across the sweep), the fold costs `drums` about 3 dB and `other`
about 3 dB of relative level, while `vocals` gains ~2 dB:

| | k = 1.00 | k = 0.00 |
| --- | --- | --- |
| vocals, relative to input | −6.5 dB | −4.4 dB |
| drums, relative to input | −12.0 dB | −15.3 dB |
| other, relative to input | −2.3 dB | −5.1 dB |
| bass, relative to input | −48.6 dB | **−13.3 dB** |

**The fold does not put the whole low end back in `bass`.** It moves 16% of the
source's below-250 Hz energy there, up from 0.002%; `other` still holds 41%.
What the fold buys is a `bass` stem that exists and is 91.6% low-band content
— against one that was digital-silence-adjacent — not a clean split. The
four stems still reconstruct the mixture at **+0.999** at every `k`, so nothing
is lost either way; the question was only ever where the low end is *assigned*.

### Verified through the shipped control, not only through the harness

The sweep above narrows the audio in the measurement harness. The shipped
`mono` path narrows it inside `apply_stereo_handling`, so the last step was to
run the **shipped control** on the same file: `DemucsSeparator.separate()`
twice, same weights, `cuda:0`, once with a default `SeparationConfiguration` and
once with `stereo_handling="mono"`.

| stem | default | `stereo_handling: "mono"` |
| --- | --- | --- |
| vocals | −23.62 dBFS, 2 ch | −23.66 dBFS, **1 ch** |
| drums | −29.08 dBFS, 2 ch | −34.48 dBFS, **1 ch** |
| **bass** | **−65.67 dBFS**, peak 176/32767 | **−32.55 dBFS**, peak **7,788**/32767 |
| other | −19.40 dBFS, 2 ch | −24.32 dBFS, **1 ch** |

That is 028's −65.7 / −32.6 and its peak of 176, reproduced through the control
this PR adds — and 0.2377 peak on the fold, the same figure 028 recorded. It
also matches the sweep's `k = 0.00` row to within 2 of 32767 (the harness writes
one extra 16-bit WAV; the shipped path does not). `Stem.channels` reports `1`
and the files on disk really are one channel, so the result record describes
what was separated.

**This table was re-measured after code review moved the fold out of torch**,
rather than carried over: changing the implementation changes the tie-break, so
the old numbers could no longer be assumed to describe the shipped code. Every
level and peak above is unchanged at the precision published — rms to 0.01 dB,
peak to four decimals — and the `as_is` run is bit-identical, as the identity
path must be. Three of the four folded stems moved by **one** int16 count
(`bass` 7,789 → 7,788, `vocals` 15,704 → 15,705, `other` 14,903 → 14,902),
which is exactly the 1-LSB tie difference predicted above and nothing else.

`tests/test_stereo_handling.py` additionally asserts that the fold *is* the mid
component of the mid/side pair, which is what keeps the shipped `mono` path and
the `k = 0.00` row the same transform rather than two that happen to agree.

## Why a stereo-handling option does not violate ARCHITECTURE.md §1

§1 says the ML model is a replaceable inference backend, and that
model-specific details — segment size, overlap, FFT size, batch size — live
behind the separation-engine interface and, optionally, in the catalog's
`default_inference_parameters`. Application code works in model IDs,
capabilities, modes, stems, jobs, devices and results, and never in the terms of
a particular network.

`stereo_handling` looks like a violation because it was *discovered* through one
architecture's failure. It is not one, for three reasons:

1. **It is a fact about the input, not about the network.** "This recording is
   an early stereo mix with near-independent channels" is true of the file
   before any model is chosen, and stays true if every model is replaced
   tomorrow. Compare `segment: [39, 5]` or `stft_n_fft: 2048`, which mean
   nothing except to one checkpoint and are meaningless to state without naming
   it. The user is answering a question about *their record*.
2. **Nothing in the contract, the UI or the state names an architecture.** The
   enum is `as_is` | `mono`; the transform is `PcmAudio → PcmAudio` in
   `inference/stereo.py` with no model in scope; the picker's labels are "Keep
   stereo" and "Fold to mono" and its note says what happens to the audio
   ("the stems come back mono"), never which network benefits. A build with a
   completely different backend serves the same field with the same meaning and
   needs no change. Frontend tests assert the notes make no quality promise at
   all.
3. **It is applied to the mixture, not passed to the model.** It happens once,
   in the shared skeleton, between decode and the chunk loop — the same place a
   resample or a channel downmix happens. `_run_chunks` and `_finish_stems`
   never see the setting; they see audio.

The §1 test that actually discriminates is: *would this option be meaningless,
or need re-explaining, if the backend were swapped?* `overlap: 0.25` would.
"Fold my wide-stereo record to mono before separating it" would not — it is the
same request of any separator, and every separator in use is trained on centred
stereo, so it is not even architecture-*specific* evidence.

The related rule this feature is careful about is a different one:
**nothing is applied that the user did not ask for.** The default is `as_is`
and is identity — `apply_stereo_handling` returns *the same object*, not an
equal one — so an existing job separates bit-for-bit as before. Detection is
not implemented, and if it ever is it must suggest and never apply (the brief,
feature 032, and the fake-separator honesty rules all say the same thing).

## Scope

- **`backend/src/straticate/schemas/jobs.py`** — `StereoHandling` (`as_is`,
  `mono`) and the `stereo_handling` field on `SeparationConfiguration`, with a
  default that keeps the field optional on the wire.
- **`backend/src/straticate/schemas/__init__.py`** — re-export.
- **`backend/src/straticate/inference/stereo.py`** — new. One pure function,
  `apply_stereo_handling(PcmAudio, StereoHandling) -> PcmAudio`, in plain
  integer arithmetic so **every** separator can call it (see *Why the transform
  is not torch* below).
- **`backend/src/straticate/inference/torch_separator.py`** — apply it
  immediately after the decode, off the event loop, so both torch backends get
  it from the shared skeleton (feature 039's whole point) and `_run_chunks`,
  `_finish_stems` and the encoder all agree about what was separated.
- **`backend/src/straticate/inference/fake.py`** — the same call, at the same
  point. The development engine's *audio* is a placeholder; what it reports
  about its own behaviour must still be true.
- **`frontend/src/api/generated/api.d.ts`** — regenerated. **Not hand-edited.**
- **`frontend/src/api/jobs.ts`** — the presentation table over the generated
  union, following `api/export.ts`'s `EXPORT_FORMAT_TABLE` idiom exactly.
- **`frontend/src/api/types.ts`** — the `StereoHandling` alias.
- **`frontend/src/state/appState.tsx`** — `configure.stereoHandling`, its
  action and its reducer case.
- **`frontend/src/components/SeparationOptions.{tsx,css}`** — the radio group,
  and `stereo_handling` on the create-job body.
- `backend/tests/test_api_jobs.py` — the echoed-configuration assertion gains
  the field, plus a round trip and a rejection of an unknown value.
- `docs/contracts/rest-api.md`, this document, one cross-reference in
  `docs/features/028-demucs-four-stem.md` (see *Two things worth being honest
  about* — a reader of 028 must not be left with the stronger claim), and the
  ROADMAP ledger row.

## Out of scope

- **Detection and suggestion.** The brief called it the most useful version and
  the most work, and it is genuinely tempting: L/R correlation is cheap and the
  audio is already decoded for `ffprobe` metadata. It is not here because it
  belongs in `backend/src/straticate/audio/` and on the `AudioFile` contract,
  neither of which this feature owns, and because a *suggestion* needs somewhere
  to live in the upload step's UI. **Nothing here guesses in the meantime.**

  **What a detection feature gets handed, concretely**, so it does not have to
  re-derive any of it:

  | | |
  | --- | --- |
  | signal | Pearson correlation of the two decoded channels, full band, whole track. One pass over audio that is already decoded; no filtering needed |
  | the failing case | **+0.23** on this track, against **0.7–0.95** for the modern productions 028 sampled |
  | a defensible threshold | **below ~+0.5**. The sweep shows the model behaves as if in-distribution from about +0.86 up and is unrecoverable below it, so +0.5 sits well clear of both edges and will not fire on ordinary stereo |
  | corroborating signal, if one is wanted | sub-250 Hz L/R correlation (+0.305 here) and the L/R low-band level imbalance (**7.5 dB** here). Neither is necessary; both are one FFT |
  | what it must say | that a stem *may* come out near-silent and that folding to mono recovers it — never that folding separates better, which is not what was measured |
  | what it must not do | apply anything. The control already exists and defaults to `as_is`; a suggestion sets nothing |

  The one thing it should measure for itself is the false-positive rate on a
  handful of ordinary modern tracks. Everything above is from **one** record.
- Automatic application without the user's knowledge — explicitly ruled out.
- Per-stem stereo reconstruction; any change to the models; mid/side narrowing
  as a shipped control (measured, rejected, recorded above).
- `frontend/e2e/**` and `playwright.config.ts` — feature 044 owns them
  concurrently. See *Known limitations*.

## Expected modules/files

- `backend/src/straticate/inference/{stereo.py (new),torch_separator.py,fake.py}`
- `backend/src/straticate/schemas/{jobs,__init__}.py`
- `backend/tests/{test_stereo_handling.py (new),test_api_jobs.py,test_api_results.py}`
- `frontend/src/api/{jobs.ts,types.ts,generated/api.d.ts}`,
  `frontend/src/api/jobs.test.ts`
- `frontend/src/state/appState.{tsx,test.tsx}`
- `frontend/src/components/SeparationOptions.{tsx,css,test.tsx}`
- `frontend/src/{test/fixtures.ts,api/types.test.ts}`
- `docs/contracts/rest-api.md`, `docs/features/041-mono-folddown-option.md`,
  `docs/features/028-demucs-four-stem.md` (one cross-reference to the two
  corrections above), `ROADMAP.md`

## Acceptance criteria

- [x] Measured comparison of at least fold-to-mono against mid/side narrowing on
      the same wide-stereo material, with the numbers recorded — seven points of
      `k`, two independent metrics, and the two known rows reproduced
- [x] The chosen control is exposed as a job configuration option, documented in
      the REST contract, with `api.d.ts` regenerated
- [x] Default behaviour is unchanged — existing jobs separate exactly as before
      (asserted as object identity in the transform, and as byte-identical stem
      files end to end on both torch backends)
- [x] If detection is implemented, it *suggests* and never silently applies —
      **not implemented**; nothing detects and nothing applies unasked
- [x] All gates green

## Required tests

`backend/tests/test_stereo_handling.py`, in two halves.

**The transform.** `as_is` returns the very object it was given (`is`, not
`==`); a request that omits the field defaults to `as_is`; `mono` folds a known
input to a known output with the channel count, sample rate and frame count all
stated; it rounds to nearest rather than flooring (the DC offset a `//` would
introduce); it cannot overflow at full scale; an already-mono source is returned
untouched; and the fold equals the mid component of the mid/side pair, which is
what keeps the shipped path and the measured `k = 0` row the same thing.

**The wiring**, parametrised over **all three** separators — both torch backends
*and the fake engine*, which is the parametrisation code review's medium finding
added. A job asking for `mono` gets one-channel stems and a result record that
says `channels == 1`; a configuration that never mentions the field and one that
names `as_is` explicitly produce **byte-identical** stem files, still stereo; and
a folded RoFormer run's `vocals + instrumental` still reconstructs the folded
mixture to within a rounding step — the assertion that catches a fold applied
after the residual is derived, or to a copy.

**The client-visible contract**, in `test_api_results.py`: a **fake-backed**
server given `stereo_handling: "mono"` returns one channel on every stem of the
result record *and* in the WAV headers of the bytes it serves, while the default
still returns two. That is the test that would have caught the medium finding,
and it is at the level the finding was about — what a client is told.

Frontend: the picker offers both choices with "Keep stereo" preselected and each
one accessibly *described* rather than renamed; choosing the fold posts
`stereo_handling: "mono"`; the choice survives changing mode and tier (it is not
a catalog value); starting without touching the control posts `"as_is"`; the
reducer records, preserves across a catalog reload, **survives a failed catalog
fetch and the retry that follows it**, and resets with the upload; **a
single-channel upload is told the control does not apply and is offered no
radios**; and the presentation table is exhaustive over the generated union and
makes no quality promise.

Every one of those was **mutation-verified** rather than merely written:
reverting the transform to a copy plus `sum/2/floor` fails all five transform
tests that should catch it; removing the `apply_stereo_handling` call from
`TorchSeparator._separate` fails all three wiring tests on both backends;
removing it from `FakeSeparator` fails `test_mono_handling_yields_mono_stems`
with exactly the reported symptom (`channels=2` after asking for `mono`);
dropping the `stereoHandling` carry from `modesFailed` fails the catalog-retry
test; and forcing the mono-upload check to `false` fails the "offers nothing to
choose" test. A test that passes against the broken code is not coverage.

Separation *quality* is deliberately not in CI: it needs real weights and real
music. The measurement above is the evidence, and it is the integration tier's
kind of work.

## Notes / decisions

### Where the fold happens, and why there

In `TorchSeparator._separate`, on the line after the decode, dispatched with
`asyncio.to_thread`. Three consequences that were the point of choosing it:

- **Both backends get it from one place.** Feature 039 extracted this skeleton
  precisely so a change like this is written once; RoFormer and Demucs need no
  edit at all, and a third backend inherits it.
- **`source` *is* the mixture from that line on.** RoFormer derives
  `instrumental` by subtracting `vocals` from `source`; Demucs' normalization
  and the encoder both read it. Folding anywhere later would leave one of them
  subtracting a stereo mixture from a mono estimate. A test asserts the
  reconstruction rather than trusting the reading.
- **The stems come back in the layout that was actually separated.** The shared
  bridge's `to_source_channels` folds the network's stereo output down to the
  source's channel count, so a folded job writes mono WAVs and reports
  `channels: 1`. That is honest: a two-channel file with two identical planes
  would claim a stereo image that no longer exists, and would cost twice the
  disk to do it.

### Why the transform is not torch — and the bug that settled it

The first version of this module crossed feature 039's existing
`torch_audio` bridge, because the arithmetic is a mean over 7.2 million 16-bit
frames and torch does that in milliseconds. **That was wrong, and code review
caught what it cost.**

Torch is an *optional* extra (feature 034), so a module that imports it is
reachable only from `TorchSeparator`. `FakeSeparator` therefore never folded:
a fake-backed server — which is what the end-to-end tier, CI and a development
checkout actually run — accepted `stereo_handling: "mono"`, answered `201`,
echoed `"mono"` back on `Job.configuration`, and returned **two-channel stems**.
The UI offers the control for every tier and posts it regardless of
architecture, so the user was told a fold had happened that had not.

The tempting fix was to weaken the contract sentence. The right one was to make
the contract true, and it is squarely feature 032's principle: 032 exists
because the fake separator was presenting fixture audio as real separation, and
a fake path reporting `channels: 2` after being asked to fold is the same
failure in miniature — the application asserting something about its own
behaviour that is not so. The fake engine's audio is honestly a placeholder;
its *self-description* still has to be true.

So the fold came back out from behind the bridge and is now plain integer
arithmetic that every backend calls. The cost was measured rather than
estimated: **3.9 s** for this 2:43 track (7.19 M frames), against milliseconds
in torch. It is paid only when a user explicitly asks for the fold — the default
returns the very object it was given without touching a sample — it runs in a
worker thread, and it sits beside a separation that takes tens of seconds on a
GPU and minutes on a CPU. A contract that is true on every backend is worth
more than three seconds on an opt-in path.

**Two things fell out of doing it in integers, and both are improvements.**

The first is a bug this feature nearly shipped. Writing the fold as
`(L + R) // 2` — or as the "obvious" `s + 1 if s >= 0 else s - 1` correction —
biases *even negative* sums downwards: a sum of −2 gives −2 where the exact
mean is −1. That is a DC offset on roughly a quarter of all frames. It was
caught by the rounding test written for the original torch version, which is
the whole argument for writing that test.

The second is that the result is now **exact**. Ties (an odd sum, about half of
all real frames) are broken **to even**, matching `torch.round` and therefore
`tensor_to_pcm`, which quantizes every stem this application writes — so the
mixture and the stems are quantized by one rule. Checked exhaustively against
every reachable two-channel sum, and against 200 k random frames at three and
four channels. The float32 version this replaced disagreed with the exact mean
by one LSB on about **9% of frames**, always on those ties, resolved by
whichever way float error happened to fall; measured over 200 k adversarial and
random frames, neither version was ever *farther* from the true mean than the
other, and the maximum difference was 1 LSB. The integer version is simply
deterministic where the float one was not.

### The field is required in TypeScript, and that is not an accident

`openapi-typescript` emits a schema property that carries a `default` as
**non-optional**, which is why the generated `SeparationConfiguration` has
`stereo_handling: "as_is" | "mono"` with no `?` while `device_id` (nullable, no
`default`) keeps its `?`. The same is already true of `Model.development_only`
on `dev` today, so this is the repository's established generator behaviour
rather than something this feature introduced.

It was left that way deliberately instead of being softened to
`StereoHandling | None`:

- the wire contract is unchanged — the field is **not** in the schema's
  `required` list, so a client may omit it and a pre-041 client is unaffected;
- `Job.configuration.stereo_handling` is then always present in responses and
  events, the same property `device_id` gets by being resolved in
  `create_job` — which is the more useful shape for a UI reading a job back;
- and it avoids two spellings of one thing on the wire (`null` and `"as_is"`),
  which is the failure mode `| None` would have introduced.

The one cost is that the frontend must state the choice on every create-job
request. It does, and the comment at that call site says why: this control
changes the user's audio, so the request should say what was asked for rather
than lean on a default the browser cannot see.

### The picker's wording is a contract of its own

This is a control that **recovers a stem you would otherwise lose**, not one
that separates better, and the measurement is what makes that distinction real
rather than pedantic: the four stems reconstruct the mixture at **+0.999**
whether the mix is folded or not, so nothing is gained overall — a near-silent
stem becomes usable because the low end is *reassigned*. The note says exactly
that ("this recovers a stem that would otherwise come out near-silent. It does
not otherwise change how well the parts are told apart") and no more, and a unit
test holds the line (`not.toMatch(/improve|better|best|fix/i)`).

The other half of the reason is sample size. This feature measured **one
track** — a real, reproduced 33 dB, but not a population — and a UI that
promised "better separation" would be making a claim the app cannot keep for an
arbitrary mix. Same reasoning as feature 032's refusal to present fake output as
real.

## Known limitations

- **The fold costs ~3.9 s of CPU on a 2:43 track**, in a worker thread, because
  it is deliberately not torch (see above). Longer material scales linearly. If
  that ever matters, the fix is a fast path *inside* `apply_stereo_handling`
  when torch happens to be importable — not moving the function back behind the
  bridge, which is what caused the contract to be false in the first place.
- **No detection, so the user has to know to look.** This is the honest gap.
  Someone separating an early stereo record gets a near-silent stem, and nothing
  on screen connects that to the control that fixes it. The follow-up is
  well-defined — L/R correlation at upload, on the `AudioFile` contract, shown
  as a suggestion in the upload or configure step — and the measurement above
  supplies the threshold; it was left out because it belongs to files this
  feature does not own.
- **No E2E coverage.** `frontend/e2e/**` and `playwright.config.ts` are feature
  044's, concurrently, so they were not touched. The control is covered by unit
  tests on both sides of the contract, by the reducer tests, and — since review
  — by an API-level test that runs a **fake-backed** job with `"mono"` and
  asserts one-channel stems in the result record *and* in the WAV headers. A
  Playwright case would now be straightforward for whoever owns that tier next,
  because the fake engine folds for real.
- **One track.** Every number here is from the same 2:43 mix feature 028 used —
  which is the right material for a *comparison* (it is the failure case) but is
  not a survey. The claim this feature makes is "on material like this, the fold
  recovers the stem and narrowing does not", not "the fold is good for stereo
  material in general".
- **`bass` is recovered, not fixed.** 16% of the source's low band, against
  0.002%. The stem exists and is usable; the low end is still mostly in `other`.
- **The fold is uniform across the spectrum.** A band-limited fold — mono below
  some crossover, stereo above — is the obvious next idea and is *not* measured
  here. It would keep most of the perceived image (low frequencies carry little
  of it) while giving the model a centred bass, and on this evidence it is the
  most promising unexplored option. It was out of scope: the brief asked for
  fold-versus-narrowing, and inventing a third transform to ship would have
  meant shipping an unmeasured one.
- **A mono upload is told the control does not apply**, rather than being
  offered a choice that cannot do anything. Both values are documented as
  identical no-ops on a single-channel source, and the fold's note claims it
  recovers a stem, so showing it there would promise an effect that cannot
  happen. The channel count comes from the upload's own probed metadata.

## Noticed, out of scope

- **`dev`'s `frontend/src/api/generated/api.d.ts` was stale before this
  branch.** Regenerating picked up a `ModelRequirements` description change from
  feature 038 (PR #55) that was merged without regenerating. This PR's
  `api.d.ts` therefore contains that hunk as well as its own. It is a
  correction, not a change — but it is the drift AGENTS.md and the merge-time
  routine warn about, arriving again, and the generated file is never
  hand-edited so it could not be separated out.
