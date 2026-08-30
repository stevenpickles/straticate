# [063] Wide-stereo detection

Branch: `063-wide-stereo-detection`
Status: PR OPEN
Dependencies: 041
PR: —

## Objective

Measure how independent an upload's two channels are, and serve the number —
implementing feature 041's detection handoff. The suggestion that number exists
to support is **built and held disabled**, because the one thing 041 asked a
detection feature to establish for itself is the false-positive rate on ordinary
records, and that measurement needs music this repository cannot hold.

## What 041 handed over, and what happened to each row

041's *Out of scope* section left a six-row table. This feature is that table,
with the last two columns filled in.

| 041 said | 063 did | where |
| --- | --- | --- |
| **signal**: Pearson correlation of the two decoded channels, full band, whole track; one pass | exactly that, streamed, in exact integer arithmetic | `backend/src/straticate/audio/analysis.py` |
| **the failing case**: +0.23 on 041's track, against 0.7–0.95 for modern productions | **reproduced: +0.22942** through the shipped endpoint (below) | `backend/tests/test_stereo_analysis.py` |
| **a defensible threshold**: below ~+0.5 | `WIDE_STEREO_THRESHOLD = 0.5`, hardcoded with 041's provenance in its docstring | `audio/analysis.py` |
| **corroborating signal, if one is wanted**: sub-250 Hz correlation, low-band imbalance | **not shipped** — see *Out of scope* | — |
| **what it must say**: a stem *may* come out near-silent; folding recovers it; never "better" | written, wording-tested, and **not shown to anyone yet** | `frontend/src/components/WideStereoNote.tsx` |
| **what it must not do**: apply anything | nothing applies; there is no code path from the measurement to a `stereo_handling` value, and both a reducer test and a UI test assert it | `appState.test.tsx`, `SeparationOptions.test.tsx` |

041 also wrote: *"The one thing it should measure for itself is the
false-positive rate on a handful of ordinary modern tracks. Everything above is
from **one** record."* That is the sentence this feature could not discharge, and
the hold below is the honest answer to it.

## The hold, and why the backend still ships

The suggestion is gated by a single module-level constant:

```ts
export const WIDE_STEREO_SUGGESTION_ENABLED = false
```

with an exported `WIDE_STEREO_SUGGESTION_HOLD_REASON` naming the unmet
precondition, and a test that pins both. Flipping the constant is the **whole**
of the follow-up: the component, its wording, its accessibility and its
"applies nothing" guarantee are all implemented and covered (the tests exercise
it with the flag forced on through a prop that nothing in the application
passes).

The backend endpoint ships anyway, and the asymmetry is the point rather than an
inconsistency:

- `GET /audio/{id}/analysis` asserts a **measurement** — *this file's channels
  correlate at 0.23* — which is true whatever the false-positive rate turns out
  to be, and which a developer, a future feature or a curious user can act on.
- The note asserts a **judgement** — *your recording is one of the ones that
  needs attention* — which is exactly the claim an unmeasured false-positive
  rate cannot support. Feature 032 exists because the application presented
  something as true that was not; putting a sentence about someone's own
  recording on screen with no evidence about how often it is wrong would be the
  same mistake in a smaller place.

## The false-positive measurement protocol

This is the protocol whoever unblocks the suggestion should run. It is written
here in full so it does not have to be re-derived, and so the acceptance
criterion is fixed **before** the data is seen.

**Material.** At least **N = 10** ordinary modern tracks (target **20**),
supplied by the user, not chosen for this test. "Ordinary" means commercially
released, mixed after roughly 1975, and *not* selected for being wide — the
population the threshold must not fire on is "records people actually separate",
so the sample must be drawn the same way. Each track is listed in this document
by **title, artist and year** only. **The audio is never committed**, in this
repository or any other, for the same reason 041's track is not.

**Method.** Upload each track through the running application and read
`GET /api/v1/audio/{id}/analysis`. Not a script that re-implements the
arithmetic: the thing being measured is the shipped endpoint's behaviour on
real material, and a harness that agrees with the endpoint on synthetic data can
still disagree with it on a container it decodes differently. Record every
`l_r_correlation` to three decimals, in the table below, alongside the track.

**Acceptance.** **Zero** false fires — every track measures **at or above
+0.5**. State the bound this earns explicitly: with 0 failures in N trials the
rule of three puts the 95% upper confidence bound on the true false-positive
rate at about **3/N**, i.e. ~30% at N = 10 and ~15% at N = 20. That is a weak
bound, and saying so is the point: it is enough to justify a note that suggests
and applies nothing, and it would not be enough to justify anything that acted.
N = 20 is the target for that reason.

**Stop conditions**, both of which mean *do not flip the flag*:

1. **Any ordinary track measures below +0.5.** The hold stands. What follows is a
   threshold question, not a UI question: 041's evidence puts the model's
   in-distribution edge near +0.86 and the failure case at +0.23, so there is
   room to move the threshold down — but moving it is a new measurement, with
   its own record, not an adjustment made to get a green result.
2. **041's known track stops reproducing +0.229.** Then the implementation is
   wrong and nothing built on it may be trusted — including any ordinary-track
   numbers gathered in the same session. Fix the implementation first and
   re-measure everything.

**The table to fill in.** Empty by design: this feature did not have the
material.

| # | track | year | `l_r_correlation` | ≥ +0.5? |
| --- | --- | --- | --- | --- |
| | *(not yet measured — the audio is the user's to supply)* | | | |

## What *was* measured

### The known wide track: 041's figure, reproduced

041's own 1968 mix (`beatles-back-in-the-ussr.m4a`, kept at the repository root
and gitignored), uploaded through `POST /api/v1/audio` and measured through
`GET /api/v1/audio/{id}/analysis` on the shipped path:

| | |
| --- | --- |
| source | 2:43, AAC in MP4, 44.1 kHz stereo, 368.6 kbps, 7,553,989 bytes |
| decoded at | 44,100 Hz — the file's **native** rate, from its probed metadata; no resample |
| 041 published | **+0.229** |
| 063 measured | **+0.22942** (`0.22942123650811655`) → **+0.229** to 041's precision |
| `wide_stereo` | `true` |
| wall clock | **1.24 s** for the whole pass, decode included |

That is the row 041 called "the load-bearing number", reproduced by an
independent implementation reading a different code path, and it is the
assertion in `test_known_wide_track_reproduces_published_figure` (marked
`integration`, deselected by default, skipped where the file is absent).

### Block size, and the latency it owes the event loop

`ANALYSIS_BLOCK_FRAMES = 8192`, sized the way feature 045 sized
`FOLD_BLOCK_FRAMES`: by the GIL hold one `asyncio.to_thread` hop costs the
event loop, not by the memory bound. Measured on 4 M stereo frames (90.7 s of
44.1 kHz audio), median per block and total over three runs:

| block frames | per block | whole pass |
| --- | --- | --- |
| 262144 | 39.29 ms | 0.61 s |
| 65536 | 9.29 ms | 0.58 s |
| 32768 | 4.91 ms | 0.60 s |
| 16384 | 2.36 ms | 0.59 s |
| **8192** | **1.20 ms** | **0.62 s** |
| 4096 | 0.58 ms | 0.64 s |
| 2048 | 0.29 ms | 0.59 s |
| 1024 | 0.15 ms | 0.69 s |

The total column is flat across a 256× range — the hops are free at this scale,
exactly as 045 found for the fold — so the per-block column is the only thing
choosing, and 8192 is the largest block still inside the ~1 ms band 045
established by probing a request served during a job against one served idle.

That works out at **0.39 s of CPU per minute of audio**: about a second for a
three-minute track, about 23 s for an hour-long one, plus FFmpeg's decode.

### The synthesized proxy, which is what runs in CI

No music in CI, so the arithmetic is pinned against constructed signals:
`shared + k·independent` pairs at target correlations
`r ∈ {0.3, 0.45, 0.5, 0.55, 0.7, 0.9}`, each checked **against
`statistics.correlation` on the very same samples** (an independent
implementation, to ~1e-9) as well as against the construction. Plus the exact
cases the tolerance-based ones cannot pin: an integer-exact +0.5 fixture for the
threshold tie, ±1.0 on identical and inverted channels, blocked-equals-whole
accumulation over four block sizes, and both documented no-correlation edges.

**What the proxy cannot establish** is the false-positive rate, and no amount of
synthetic material can: the question is what real records measure, and that is
what the protocol above is for.

## Scope

### Backend

- **`backend/src/straticate/audio/analysis.py`** — new. The five-sum streaming
  correlation, `WIDE_STEREO_THRESHOLD` with 041's provenance,
  `ANALYSIS_BLOCK_FRAMES` with the table above, `analysis_from_sums` (the single
  place `wide_stereo` is derived), `StereoAnalysisCache` (single-flight,
  in-process), and `AudioAnalysisError`.
- **`backend/src/straticate/schemas/audio.py`** — `StereoAnalysis`
  (`l_r_correlation: float | null`, `wide_stereo: bool`), re-exported from
  `schemas/__init__.py`.
- **`backend/src/straticate/api/audio.py`** — `GET /{audio_id}/analysis`, and
  `DELETE /{audio_id}` gains the cache drop.
- **`backend/src/straticate/main.py`** — the cache on `app.state`, per
  application.
- **`docs/contracts/rest-api.md`** — the route, the record, both null cases, the
  timeout row; **`backend/openapi.json` → `frontend/src/api/generated/api.d.ts`**
  regenerated (see *Notes*).

### Frontend (implemented, held)

- **`frontend/src/api/{audio,types}.ts`** — `getAudioAnalysis`, the
  `StereoAnalysis` alias.
- **`frontend/src/state/appState.tsx`** — the `analysis` slice
  (`idle | loading | loaded | failed`), its three actions, and its reset with the
  upload.
- **`frontend/src/components/useStereoAnalysis.ts`** — the fire-and-forget read,
  once per audio ID, stereo uploads only.
- **`frontend/src/components/WideStereoNote.{tsx,test.tsx}`** — the suggestion,
  its wording, and the hold.
- **`frontend/src/components/SeparationOptions.{tsx,css}`** — the hook and the
  note inside the Stereo fieldset.

## Out of scope

- **Enabling the suggestion.** The measurement that would justify it is the
  user's to unblock; the protocol is above.
- **The corroborating signals.** 041 offered the sub-250 Hz correlation
  (+0.305 on its track) and the low-band L/R imbalance, and called neither
  necessary. Neither is computed. Each costs an FFT and a second threshold, the
  full-band number is the one 041 measured a *distribution* for, and there is a
  recorded disagreement between 028 (5.8 dB) and 041 (7.5 dB) about the
  imbalance on the same track — which is a reason to understand it before
  deciding anything with it, not a reason to average them.
- **Applying anything, ever.** Including "apply and tell the user". 041, 032 and
  the fake-separator honesty rules all say the same thing.
- **062's files beyond the note's wording.** The picker, its table and its notes
  are 062's; this feature reads them and changes none of them.
- **A durable or shared cache.** The record is two scalars and recomputing it
  costs one bounded pass; a sidecar would be a second thing to keep in step with
  the file.

## Expected modules/files

- `backend/src/straticate/audio/{analysis.py (new),__init__.py}`
- `backend/src/straticate/schemas/{audio,__init__}.py`
- `backend/src/straticate/api/audio.py`, `backend/src/straticate/main.py`
- `backend/tests/{test_stereo_analysis.py (new),test_export_openapi.py}`
- `frontend/src/api/{audio.ts,audio.test.ts,types.ts,generated/api.d.ts}`
- `frontend/src/state/appState.{tsx,test.tsx}`
- `frontend/src/components/{WideStereoNote.tsx (new),WideStereoNote.test.tsx (new),useStereoAnalysis.ts (new),SeparationOptions.tsx,SeparationOptions.css,SeparationOptions.test.tsx,AudioSummary.test.tsx,Workspace.test.tsx}`
- `docs/contracts/rest-api.md`, this document, `ROADMAP.md`

## Acceptance criteria

- [x] Full-band Pearson L/R correlation, whole track, one streaming pass at the
      file's native sample rate, with flat memory at any length
- [x] `GET /api/v1/audio/{audio_id}/analysis` serving `StereoAnalysis`, computed
      once, single-flighted across concurrent first requests, dropped on
      `DELETE`, `404` on an unknown ID
- [x] `wide_stereo` derived server-side from a single hardcoded threshold whose
      docstring carries 041's provenance
- [x] Both documented no-correlation cases answered as decided (mono →
      `{null, false}`; zero-variance channel → `{null, true}`)
- [x] Contracts regenerated: `rest-api.md`, OpenAPI, `api.d.ts` committed
- [x] The suggestion implemented, wording-tested, and **disabled** behind one
      constant whose reason names the unmet precondition; a test pins that it is
      off and why
- [x] The suggestion applies nothing: the analysis arriving dispatches no
      selection change, asserted at both the reducer and the component level
- [x] 041's +0.229 reproduced through the shipped endpoint on the real track
- [ ] **False-positive rate measured on ordinary tracks** — *not done; the
      material is the user's to supply. This is the criterion the hold exists
      for, and the reason the ledger row does not claim detection is shipped.*
- [x] Backend quartet and frontend five gates green

## Required tests

**Backend** (`backend/tests/test_stereo_analysis.py`, 36 tests):

- the synthesized proxy at six target correlations, each against
  `statistics.correlation` and against its construction;
- `wide_stereo` follows the threshold at every one of them; the integer-exact
  +0.5 fixture is **not** wide (strictly below) and the same pair one frame
  shorter is;
- ±1.0 exactly on identical and inverted channels;
- blocked accumulation equals whole-track accumulation at 1, 7, 1024 and 8192
  frames per block, and a truncated trailing frame changes no sum;
- both no-correlation edges, and a mono source answered **without launching a
  subprocess** (asserted by the file not being read at all);
- the endpoint on wide, ordinary and mono uploads; its answer equal *to the last
  bit* to the in-process measurement of the same samples;
- `404` on an unknown ID and on a file gone from disk; `422` when FFmpeg cannot
  decode bytes ffprobe accepted; `504` `audio_analysis_timed_out` with
  `detail.timeout_seconds`;
- single flight (the loader runs **once** for two concurrent first requests,
  gated deterministically on the first having entered it), a cached second
  request that must not recompute, `discard` forgetting, `DELETE` calling it,
  and a failed computation **not** becoming the permanent answer;
- the OpenAPI document carrying both the route and the schema;
- `@pytest.mark.integration`: 041's track reproducing +0.229.

**Frontend**:

- `WideStereoNote.test.tsx` — renders iff enabled **and** wide; nothing while
  loading, failed or idle; no controls at all; wording promise-free
  (`/improve|better|best|fix/i`), says *may*, names no picker label; **the flag
  is `false` and its reason names the missing measurement**; the default render
  shows nothing for a wide mix.
- `SeparationOptions.test.tsx` — a stereo upload is measured exactly once; a
  mono upload is never measured; a failed measurement is silent (no alert, no
  note, every control present); no suggestion while the hold stands; a wide
  measurement changes no radio and the posted job still says `as_is`.
- `appState.test.tsx` — the slice's transitions; `analysis/loaded` returns the
  **same `configure` object** (identity, not equality — the strongest available
  statement that nothing was applied); resets on `upload/reset` and on a new
  `upload/succeeded`.
- `api/audio.test.ts` — the path, its encoding, and the typed error.

## Notes / decisions

### Streaming, not `decode_to_pcm`

Two reasons, and the second is the one that decides the *number*.

**Memory.** `decode_to_pcm` materialises the whole track — an hour of 44.1 kHz
stereo is 635 MB of PCM before the planar split copies it again. Feature 038
established that this application is bounded in the length of a track and
feature 041 had to re-learn it inside `inference/stereo.py`; a third place
forgetting it would be a pattern. Here only six integer accumulators survive a
block.

**The sample rate.** `decode_to_pcm` resamples to the model's rate, and a
resample is a filter. 041's figure is a *full-band* correlation of the file as
released, so the decode asks for the rate ffprobe reported. That is the
difference between reproducing +0.229 and being near it.

The consequence is a second FFmpeg call shape — `subprocess.Popen` with stdout
read in blocks — which `audio/ffmpeg.py`'s single `run_ffmpeg` cannot provide,
because it captures stdout in memory by design. It follows that module's
conventions (bounded, killed on expiry, `FFmpegTimeout` rather than a decode
error) rather than its function. stderr goes to a temporary **file**, not a
pipe: a pipe with no reader deadlocks against a stdout read if FFmpeg ever
writes more than the buffer.

### Why the arithmetic is integer, and what that buys

Five sums over int16 samples are integers, and Python's are arbitrary
precision, so the accumulation cannot overflow and cannot round. The only float
operation in the whole measurement is the final division. Two things follow that
the tests use: a blocked pass is **bit-identical** to a single-block one (so the
streaming shape is not a second implementation), and ±1.0 is decided by the
integer identity `numerator² == varianceₗ · varianceᵣ` rather than by where a
square root lands.

### The layering, and one duplicated constant

`audio/analysis.py` does **not** import `straticate.inference`.
`inference/pcm.py` already imports `audio/ffmpeg.py` and `audio/probe.py`, so
reaching the other way would close a package-level cycle. The cost is an
`AudioAnalysisError` distinct from `inference.pcm.AudioDecodeError` and a
`_STEREO_FRAME_BYTES = 4` beside `SAMPLE_WIDTH_BYTES`; the API maps the error
onto the same `audio_not_decodable` the upload path uses, so a client sees one
vocabulary.

### Holding the request open

The first request blocks for one decode. That would be unacceptable for
anything else here — ARCHITECTURE.md §4 keeps inference out of request handlers
— and is acceptable for this because **the endpoint gates nothing**: the UI
fires and forgets it, every control works while it is outstanding, and a client
that never calls it loses no function. Giving it the job machinery would put a
progress bar and a WebSocket channel around a number. The honest limitation is
that an hour-long file holds one connection for something over half a minute;
it is recorded below rather than engineered away.

### `analysis` beside `upload`, not inside it

The assignment described this as `upload.analysis`. `UploadState` is a union
discriminated on the upload's own progress (`idle | uploading | uploaded |
error`), and a measurement that arrives after the upload finished is not a stage
of uploading — nesting it would have meant either widening every arm or making
every existing `{status: 'uploaded', file}` literal in the suite carry a field
about something else. It is therefore `AppState.analysis`, with the behaviour
the assignment asked for: it belongs to the upload and resets with it, on both
`upload/reset` and a new `upload/succeeded`.

### The fetch is a hook, not a call beside the upload

Two things make an upload current — `DropZone` finishing one and `SessionGate`
restoring one after a reload (feature 033) — so a measurement fired from the
first alone would be missing for exactly the users who reloaded.
`useStereoAnalysis` is keyed on the audio ID and mounted by the configure step,
which covers both.

### `wide_stereo` on the wire, and the threshold off it

The flag is derived server-side and the threshold is deliberately **not** in the
contract. A client that re-applied it would be a second place to keep in step,
and the first bug would be a UI disagreeing with the server about what the
server measured. Same reasoning as 062's crossover: the *choice* is the user's;
the constant is the application's.

## Known limitations

- **The suggestion is not shown to anyone.** That is the whole point of the
  hold, and it means the user-visible half of 041's known limitation ("no
  detection, so the user has to know to look") is **not** yet closed. What is
  closed is that the number now exists and is served.
- **The false-positive rate is unmeasured.** Everything about the threshold
  still traces to 041's single record. The protocol above is what would change
  that, and the acceptance bound it can earn (~3/N by the rule of three) is
  weak even when it passes.
- **A long file holds its first request open.** ~23 s of CPU for an hour of
  audio, plus decode. Bounded, cancellable only by the client giving up (the
  computation then finishes for whoever else is waiting), and gating nothing.
  If it ever matters, the fix is to measure a bounded sample of the track rather
  than to move the work to the job machinery — but a sampled correlation is a
  different measurement from 041's and would need its own reproduction.
- **A completely silent stereo file is reported `wide_stereo: true`.** Both
  channels have zero variance, so the documented one-sided rule catches it. It
  is not wrong in effect — a silent file will certainly produce silent stems —
  but it is the rule firing for a reason the rule was not written about.
- **The measurement is not persisted.** A backend restart forgets it, and the
  next client to ask pays for one more pass. The record is two scalars; a
  sidecar would cost more to keep honest than it saves.
- **Wider-than-stereo sources are downmixed to two channels before measuring.**
  That matches what the separator is given (`MAX_OUTPUT_CHANNELS`), so the
  number describes the audio a model would see — but "L/R correlation" of a 5.1
  mix is a property of FFmpeg's downmix as much as of the recording.
- **No E2E coverage.** Nothing user-visible ships, so there is nothing for
  Playwright to see; the case to write is the one that appears when the flag
  flips.

## Noticed, out of scope

- **`dev`'s `api.d.ts` was current.** Regenerating after 062 and 060 produced
  only this feature's own hunks, so the "second of 062/063 regenerates" rule
  cost nothing this time. Recorded because the rule exists precisely because it
  has not always been so.

### Windows delete/analysis race (review finding, documented)

On Windows, `DELETE /audio/{id}` racing an in-flight analysis leaves the
original file and its directory behind: ffmpeg holds the file open for the
decode's duration (the first place a plain request handler holds a file open
for seconds), Windows denies delete-sharing, and `remove_files`'s
`ignore_errors` rmtree moves on. The DELETE still answers 204 and the record
is gone; the survivor is a classic orphan that `POST /system/prune` (060)
classifies and reclaims — reproduced and verified by the review. Self-healing
and requiring two specific requests to race, it is recorded here rather than
engineered around; a retry-after-in-flight-settles hook on the analysis
cache is the shape of a fix if it ever matters in practice. Also noted: the
timeout path's decoder close racing the background read thread was
stress-tested by the review (20 trials) with no hangs, zombies, or leaks.
