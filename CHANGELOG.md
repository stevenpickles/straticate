# Changelog

All notable changes to Straticate are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely — the headings
below are written for the person *running* Straticate rather than as a list of
merges — and the project follows [Semantic Versioning](https://semver.org/).

Model weights are versioned separately from the application and are never
bundled with it; the *Licensing* section of each release says what shipped in
the catalog.

## [0.3.0] — 2026-08-30

Durability, measured separation quality, and a round of timeline hardening.
Everything from [the 0.2.0 notes](https://github.com/stevenpickles/straticate/blob/v0.2.0/CHANGELOG.md)
still applies; this section covers what changed.

### What's new

**Job records and uploads survive a restart, and disk usage is visible and
reclaimable.** A completed job's record, result, stems and exports are now
reachable through the API in the next process, and a previous upload can
still be found and re-separated after a restart. A job that was queued or
running when the server stopped comes back `failed`, with the error code
`job_interrupted`, rather than vanishing or being silently re-queued — and a
failed model install now survives a restart the same way, instead of quietly
reporting the model available again. Three things follow from that:
`DELETE /jobs/{id}` removes a finished job's record, stems and exports in one
call; `GET /system/disk-usage` reports what's on disk under four headings —
uploads, job stems, job exports, orphans — alongside free and total space for
the drive; and `POST /system/prune` reclaims space in three opt-in classes
(export caches, orphaned files, old finished jobs) that never touches a job
that is still queued or running.

**A new stereo-handling option, `mono_bass`, recovers the bass stem on
wide-stereo mixes without folding the whole track to mono.** Where **Fold to
mono** (0.1.0) recovers a near-silent `bass` stem by discarding every stem's
stereo image, `mono_bass` folds only the material below 500 Hz — on the one
mix measured, it recovers slightly more of the source's low end than a full
fold (19.4% against 16.0%) at about a third of the cost to `drums` and
`other` (about 1 dB against about 3 dB), and every stem comes back in
stereo. See *What it cannot do* for the caveat this measurement carries.

**Straticate can now measure how independent an upload's left and right
channels are.** `GET /audio/{id}/analysis` reports a correlation figure and
whether a file looks wide enough to be a candidate for **Fold to mono** or
`mono_bass`. The in-app note that would point this out to a user at the
moment it matters is built and tested, but is not shown to anyone yet — see
*What it cannot do*.

**The Inspect timeline survived a round of hardening.** The audio engine,
the playhead, the loop region and the zoom/scroll window now belong to the
job you're inspecting rather than to the Inspect screen itself: leaving
Inspect and coming back restores exactly where you left off instead of
rebuilding the engine from 0:00 and re-downloading every stem, and a page
reload now restores the playhead, loop region and zoom/scroll window once
the stems finish loading again. Lane headers no longer clip their own
content at larger browser font sizes, and every per-stem level fader's
pointer target now reaches the WCAG 2.2 24 px minimum at every size tested.
Auto-follow no longer page-flips the timeline twice per pass when the
visible window is zoomed narrower than an active loop region.

### Fixed

**A failed stem-audio download is now recoverable in place.** 0.2.0 fixed a
failed *result* fetch; a stem whose audio itself failed to download still
left the Inspect step showing unhelpful text with no working control. There
is now a "Try again" button for that case too, and recovering a stem never
disturbs the stems that already loaded — their buffers, gain nodes and
playback keep running untouched.

### What it cannot do

Carried forward from what shipped in 0.2.0, plus what this release ships
with:

- **An interrupted job does not resume.** Job records now survive a
  restart, so a *finished* separation and its stems are still there
  afterward — but a job that was queued or actively running when the server
  stopped comes back `failed` (`job_interrupted`) rather than being
  re-queued or resumed. Start it again when you want it.
- **Nothing prunes automatically.** `POST /system/prune` reclaims space, but
  only when something calls it: there is no retention policy, no schedule
  and no background sweep. A file held open by an in-progress download can
  also survive a delete or a prune as debris on Windows, until whatever held
  it closes and a later prune sweeps it.
- **`mono_bass` is measured on one track, the same wide-stereo mix used to
  measure 0.1.0's "Fold to mono."** It is the right track for the
  comparison — it is the failure case — but it is not a survey, and the
  500 Hz crossover is fixed for the filter measured, not user-adjustable.
  `bass` is recovered, not cleanly separated: it still holds only 19.4% of
  the source's low-frequency energy, and `other` still holds 37.5% of it.
- **The wide-stereo suggestion is built but held disabled.** `GET
  /audio/{id}/analysis` serves a real measurement, but the note that would
  tell a user their recording is a candidate for folding is switched off
  pending a false-positive measurement on ordinary, user-supplied tracks —
  everything the threshold rests on so far is one record. Nothing about the
  measurement is ever applied automatically, disabled or not.
- **The rest of 0.2.0's caveats still stand, unaffected by this release**:
  `vocals` mode has no fast tier and runs at about 0.3× real time on a CPU
  (the licence gate on a fast model is unchanged — see *Licensing*); Demucs
  loses the `bass` stem on wide-separation stereo mixes without **Fold to
  mono** or `mono_bass`; the Demucs weights are research-use-only; a 24-bit
  or 32-bit-float export re-encodes 16-bit audio rather than recovering
  detail; there is still one job at a time with no history; model downloads
  are not resumable and installed weights are not re-verified after
  install; exports are still buffered in the browser tab with no progress
  indicator and cannot be cancelled mid-download; cancelling a running
  separation still takes effect at the next chunk boundary, not instantly;
  and there is still no model *update* path (remove and install again).
  Playhead, loop region and zoom state no longer reset on leaving Inspect or
  reloading the page — the one 0.2.0 caveat this release does resolve.

### Licensing

Unchanged from 0.1.0 and 0.2.0 — read the *Licensing* section of
[the 0.1.0 notes](https://github.com/stevenpickles/straticate/blob/v0.1.0/CHANGELOG.md)
if you have not. Nothing about the model catalog, its licences or its
weights changed in this release.

The reopen criterion for a fast `vocals` tier (feature 027 — a weights
licence stated by a party with standing to grant it) was re-checked on
2026-08-30 and still does not hold: the upstream licensing question
(`Anjok07/ultimatevocalremovergui` issue #2341) remains open and
unanswered by a maintainer, and that repository's `LICENSE` file still
404s on its default branch (issue #1798, also still open). No new
information since 027's 2026-08-25 investigation.

## [0.2.0] — 2026-08-29

The Inspect step becomes an editor's timeline. Everything from
[the 0.1.0 notes](https://github.com/stevenpickles/straticate/blob/v0.1.0/CHANGELOG.md)
still applies; this section covers what changed. (The reference matters when
you are reading this as published release notes, where the 0.1.0 section is
not on the page.)

### What's new

**A per-stem waveform timeline, drawn from the audio you already downloaded.**
Every stem gets its own lane on a shared time axis, painted from the real
decoded samples rather than a placeholder — the same picture for a two-stem
and a four-stem result, drawn by hand on `<canvas>` with **no new runtime
dependency**: the frontend still depends on exactly `react` and `react-dom`.
The timeline *is* the accessible seek control — click or drag a lane to
seek (a plain click on the ruler seeks too; dragging the ruler marks a loop
region instead — see below), or drive it from the keyboard once it has
focus: Left/Right moves a second, Shift+Left/Right five seconds, Home and
End jump to the ends, and Space plays or pauses. The old range-input
scrubber is gone.

**Zoom and pan.** Ctrl+scroll zooms about the point under the cursor; a plain
or shifted scroll pans once you are zoomed in (at full fit the wheel still
belongs to the page); "Zoom in", "Zoom out" and "Zoom to fit" in the corner
of the timeline (also `+`/`-` on the keyboard) zoom about the playhead
instead. A scroll thumb under the ruler shows and drags the visible window
against the whole file. Zoom far enough in and the lanes redraw themselves
from the underlying samples instead of the whole-file overview, so the
picture stays sharp rather than blocky.

**Audible scrubbing, Audacity-style.** Press and drag the timeline while
listening and you hear short preview grains of every stem at the position
under the pointer, respecting whatever mute, solo and level you've already
set — dragging *through* a muted stem stays silent. A plain click makes no
sound. The main playback graph is never rebuilt while you drag; only the
release moves the transport, the same one-seek-per-gesture behaviour the
timeline already had.

**Loop / A-B regions.** Drag across the ruler, or Shift-drag across the
lanes, to mark a passage; drag either edge to adjust it once it exists; or
use the transport's "Loop start", "Loop end" and "Clear loop" buttons. A
`Loop m:ss – m:ss` badge announces the current region for screen readers. The
loop is sample-accurate across every stem — all of them wrap on the exact
same sample, not on a watched clock — and it is a **trap, not a fence**: a
seek that lands past the end of the region plays straight through to the end
of the file rather than being pulled back in. One Audacity idiom to know: a
plain click on the ruler seeks *and discards* the current region — the
transport's "Clear loop" button does the same thing deliberately.

**Per-stem volume faders.** Each lane header has its own volume slider
alongside its Mute and Solo buttons, reading and writing the level
independently of whether that stem is currently muted or soloed out.

### Fixed

**A failed result fetch is now recoverable without a page reload.** 0.1.0
shipped with the Inspect step permanently stuck on "Something went wrong.
Please try again." if the one-shot request for a job's result ever dropped,
with no control that actually retried anything (0.1.0's Known Limitations, "no
control that tries again"). There is now a working "Try again" button that
refetches the result and, on success, plays the stems normally.

### What it cannot do

Carried forward from what shipped in 0.1.0, plus what the timeline ships
with:

- **Nothing about the Inspect step survives leaving it.** The playhead
  position, the loop region and the current zoom are all held in the
  timeline's own state, not the job's — step away to Configure or Export and
  back, or reload the page, and the audio engine rebuilds from 0:00 with the
  stems re-downloaded and no region set. This was already true of the
  playhead in 0.1.0; it now also covers the loop region and the zoom level.
- **The lane headers are tight at larger browser font settings, and it is
  measured, not estimated.** At a 16 px root font there is about 0.9 px of
  slack in each 64 px lane header; at the 17–20 px root fonts a user's
  browser zoom or accessibility settings can produce, the header's rows
  outgrow the box by 1–7 px and the extra is clipped. The fader inside stays
  clickable throughout (verified with `elementFromPoint`), and its own
  pointer target is about 11 px tall — under the 24 px WCAG 2.2 (SC 2.5.8)
  guideline, though keyboard operation of the fader is unaffected. The real
  fix is a taller or font-relative lane height; it is not this release's.
- **Auto-follow can page-flip twice a loop** when the visible window is
  zoomed narrower than the loop region: the window jumps forward as playback
  approaches the region's end, then jumps back when the wrap reopens it. This
  is the zoom-follow behaviour working as designed, applied to a case that
  looks busy on screen; it is not a defect on its own.
- **Retry now covers the result fetch, not a failed stem download.** If the
  audio itself fails to load after the result arrives, the Inspect step still
  shows the same unhelpful "Something went wrong" text with no control that
  does anything — the fix above widened retry to the result fetch only.
- **The rest of 0.1.0's caveats still stand, unaffected by this release**:
  `vocals` mode has no fast tier and runs at about 0.3× real time on a CPU;
  Demucs loses the `bass` stem on wide-separation stereo mixes, with **Fold to
  mono** as the verified workaround; the Demucs weights are research-use-only
  (see *Licensing*, unchanged since 0.1.0); job records live in memory and do
  not survive a restart; nothing prunes uploads, job outputs or exports; a
  24-bit or 32-bit-float export re-encodes 16-bit audio rather than recovering
  detail; there is still one job at a time with no history; model downloads
  are not resumable; exports are still buffered in the browser tab with no
  progress indicator and cannot be cancelled mid-download; cancelling a
  running separation still takes effect at the next chunk boundary, not
  instantly; there is still no model *update* path (remove and install
  again); and installed weights are still verified only at install time, not
  re-checked afterwards.

### Licensing

Unchanged from 0.1.0 — read the Licensing section of
[the 0.1.0 notes](https://github.com/stevenpickles/straticate/blob/v0.1.0/CHANGELOG.md)
in full if you have not; it is the limitation most likely to matter to you.
Nothing about the model catalog, its licences or its weights changed in this
release.

## [0.1.0] — 2026-08-26

> This section is the historical record of the 0.1.0 release, frozen as
> published. Later releases fix some of what it describes — its Known
> limitations in particular are superseded where a newer section above says
> so — and it is deliberately not edited to keep up.

The first release. Straticate separates a mixed music file into stems, in a
browser, on your own machine, with no account and no cloud service.

### What it does

**One process, one URL.** Build the frontend once; after that `python -m
straticate` serves the interface and the API together on
<http://127.0.0.1:8000>. Deep links and refreshes work. A checkout with no
built bundle still serves the whole API — the root URL tells you what to build.

**Select → Configure → Separate → Inspect → Export.**

- **Select** — drag a music file in or pick one. The file is validated on its
  actual contents rather than its extension, and probed with `ffprobe`; uploads
  up to 1 GiB are accepted by default (`STRATICATE_MAX_UPLOAD_BYTES`). Duration,
  container, codec, channels, sample rate, bit depth, bit rate and size are
  shown before you commit to anything.
- **Configure** — two separation modes, both served by the backend rather than
  hardcoded in the interface:

  | Mode | Stems | Model |
  | --- | --- | --- |
  | Vocal Isolation | `vocals`, `instrumental` | Mel-Band RoFormer (Kim Vocal 2) |
  | Standard Stems | `vocals`, `drums`, `bass`, `other` | Hybrid Transformer Demucs (htdemucs v4) |

  Each quality tier is priced where you choose it — "Installed", "Needs a
  870.8 MB download", "Downloading its weights…" — and the selected model's
  licence terms, attribution and hardware requirements are shown beside it,
  before you install anything. A **Keep stereo / Fold to mono** control decides
  what happens to the recording's stereo image before separation; the default
  keeps it exactly as it is.
- **Separate** — one job at a time, run asynchronously. Progress is real work
  (`chunks completed / chunks total`) pushed over a WebSocket, never a timer, and
  Cancel is honoured. A live telemetry panel shows the model, the compute device
  and — on CUDA — VRAM allocated and peak, plus the stage, elapsed time, audio
  processed and real-time factor. GPU utilization and temperature fill in too if
  the optional `nvidia-ml-py` binding is installed, and are simply blank if it is
  not; nothing here requires it. Reloading the page mid-job returns you to the
  job.
- **Inspect** — every stem plays in sample-accurate sync off one clock, with
  per-stem solo and mute, play/pause and a seek scrubber.
- **Export** — WAV 24-bit, WAV 32-bit float or FLAC. One stem gives you an audio
  file; two or more give you a zip of the stems plus a `separation.json`
  describing the run. Repeating an export you have already taken is served from
  a cache.

**Model weights install themselves, once, from the app.** Nothing is bundled and
nothing is redistributed: the catalog pins a download URL and a SHA-256, and the
app fetches from the publisher's own server, verifies the hash and publishes the
file atomically. It shows the download size, the licence terms and how much room
is free on the target disk before it starts, and it will warn — but never
refuse — if the margin looks tight. Installs can be cancelled and weights can be
removed.

**The GPU is found, or it is not, and either way it just runs.** Compute devices
are detected for you; a job that pins no device gets the best one available.
Installing a CUDA build of PyTorch is one documented command and changes no
code, no settings, no API and no schema.

### What you need

- **[uv](https://docs.astral.sh/uv/)** — it fetches Python 3.12 for you.
- **Node.js ≥ 20 and npm**, to build the frontend bundle once.
- **FFmpeg, including `ffprobe`, on `PATH`.** Not optional: it probes uploads,
  decodes audio for separation and transcodes exports.
- **PyTorch**, installed with `uv sync --extra torch`. This is the CPU build. A
  GPU is optional; see DEVELOPMENT.md (*PyTorch and CUDA*) for the one command
  that swaps in a CUDA wheel, and for the two ways a later `uv` invocation can
  quietly swap it back out.
- **Disk for model weights**, downloaded on first use: **870.8 MB**
  (913,106,900 bytes) for Vocal Isolation, **80.2 MB** (84,141,911 bytes) for
  Standard Stems.
- **Host RAM, which is what limits how long a track can be.** VRAM is not the
  binding constraint — since the overlap-add was moved onto the host it is flat
  with track length — but the host's memory is. The figure covers the **peak
  working set of the whole backend process while a separation runs**, not the
  audio alone, and it is linear in track length:

  | | Standard Stems (Demucs) | Vocal Isolation (RoFormer) |
  | --- | --- | --- |
  | peak working set | ≈ 1,600 MiB + 2.81 MiB per second of audio | ≈ 2,260 MiB + 2.33 MiB per second |
  | what 8 GiB covers | about **39 minutes** of audio | about **42 minutes** |
  | what 16 GiB covers | about 85 minutes | about 100 minutes |
  | a four-minute song | about 1.7 GiB | about 1.7 GiB |

  Those are fits to measured runs, and they are the *process*, so anything else
  large on the machine comes out of the same budget.
- **A GPU is optional.** With one, the catalog asks for 4,096 MiB of VRAM for
  either model (3,072 MiB minimum for Standard Stems). Those figures are
  advisory: nothing is refused for failing them.

Straticate binds to loopback and has **no authentication**. It is a local-first
tool, not a service to put on a network.

### Measured, on an NVIDIA GeForce RTX 4060 Laptop GPU (8,188 MiB) with an Intel laptop CPU

| | CPU | `cuda:0` |
| --- | --- | --- |
| Vocal Isolation, 30 s clip | 100.5 s (**0.30× real time**) | 6.7 s (**4.50×**) |
| Standard Stems, 30 s clip | 18.3 s (**1.63× real time**) | 2.2–2.8 s (10.7–13.5×) |

Peak VRAM is flat with track length: **2,980.6 MiB whole-device** for Vocal
Isolation at *any* length from 30 seconds to an hour — the same figure to the
byte — and about **1,815–1,890 MiB** for Standard Stems out to roughly 38
minutes, after which it climbs at 0.077 MiB per second of audio (2,210.6 MiB at
90 minutes). Track length is bounded by host RAM, not by the card.

Separation quality, correlated against known ground truth on a synthesised
mixture — the *mixture's* own correlation with each source is given as the
baseline that makes the numbers mean something:

| stem | against its own source | mixture baseline |
| --- | --- | --- |
| `vocals` (Vocal Isolation) | **+0.993** | +0.231 |
| `instrumental` (Vocal Isolation) | **+1.000** | — |
| `vocals` (Standard Stems) | **+0.952** | +0.324 |
| `drums` | **+0.907** | +0.221 |
| `bass` | **+0.985** | +0.568 |
| `other` | **+0.993** | +0.723 |

Read the baseline column. `other`'s +0.993 is impressive against a mixture that
already correlates with that source at +0.723, and much less so than the same
number would be for `drums`. Both figures come from synthesised mixtures where
the true sources were known — not from real records, which have no ground truth
to correlate against.

### Known limitations

These are the things worth knowing *before* you separate something, not
afterwards.

**Vocal Isolation has no fast tier, and on a CPU it is slow.** It runs at about
**0.3× real time** — roughly ten minutes for a three-minute song on a machine
with no GPU. Standard Stems is the mode with a usable CPU path at 1.63× real
time. A fast vocals model was investigated and abandoned: no party with standing
has stated a licence for the MDX-family weights that would fill the tier, and
restrictive terms are workable where silence is not.

**Demucs loses the bass stem on wide-separation stereo mixes.** On a 1968 stereo
mix whose channels are nearly independent (full-band L/R correlation **+0.229**,
against 0.7–0.95 for modern productions) the `bass` stem came back effectively
silent: **−65.7 dBFS**, peak 176 of 32767. The model is trained on material
where bass is essentially always centred. Choosing **Fold to mono** recovers it
to **−32.6 dBFS**, peak 7,788 — a 33 dB recovery, reproduced three times.

Be clear about what the fold does and does not do:

- It **recovers a stem that would otherwise be unusable. It does not fix the
  separation.** It moves **16%** of the source's below-250 Hz energy into `bass`,
  up from 0.002% — and `other` still holds **41%** of it. What you get is a
  `bass` stem that exists, not a clean split.
- It **is not free for the other stems.** Measured against each variant's own
  input level, the fold costs `drums` about **3 dB** and `other` about **3 dB**,
  while `vocals` gains about **2 dB**.
- **Every stem comes back mono**, because the mixture that was separated was
  mono. `Stem.channels` says `1` and the files really are one channel.
- **Nothing detects the condition.** You have to notice the near-silent stem and
  know that this control exists. There is no suggestion in the interface, and
  nothing is ever folded unasked.
- **All of the above is from one track.** It is the right track for the
  comparison — it is the failure case — but it is not a survey. Partial
  mid/side narrowing was measured across seven settings and rejected: there is
  no setting that both recovers the stem and leaves an audible stereo image.

**Model weights carry their own licences, and one of the two shipped models is
research-use-only.** See *Licensing* below. This is the limitation most likely
to matter to you and the one least likely to be noticed.

**Job records live in memory and do not survive a restart.** Restarting the
backend loses every job record, while the stems and exports it produced stay on
disk — so a result URL for output that still exists will answer `404`. The
interface explains this and offers to run the job again. A failed model install
is forgotten the same way; the model simply reports itself as available again.

**Nothing prunes anything.** Uploads, job output directories, stems and export
artifacts accumulate under the data directory forever. There is no retention
policy, no cleanup command and no size cap; deleting an uploaded file does not
delete the stems derived from it, and every distinct format-and-selection you
export leaves another file behind. The free-space figure the app reports is for
the **models** directory only — nothing warns about the data directory filling
up.

**A 24-bit or 32-bit-float export adds no detail.** The separator writes 16-bit
PCM, so those formats re-encode what is there rather than recovering anything.
The interface says so beside the format picker.

**Smaller edges, stated so they are not surprises:**

- One job at a time, and no job history or library — the interface holds one
  separation at a time.
- If the request that fetches a finished job's stems fails, the Inspect step
  shows "Something went wrong. Please try again." with **no control that tries
  again**; reload the page. This is a known open defect.
- Model downloads are not resumable — an install interrupted at 95% starts
  over — there is no update path (remove and install again), and installed
  weights are not re-verified after the install-time SHA-256 check.
- Cancelling a running separation takes effect at the next chunk boundary, which
  is several seconds of CPU time.
- Exports are buffered in the browser tab with no progress indicator, and a
  download in progress cannot be cancelled.
- The stem player has no per-stem volume, no keyboard transport and no
  scrub-while-playing preview, and it re-downloads the stems if you leave the
  Inspect step and come back.

### Licensing

**Straticate itself is MIT.** Its model weights are not, and a model's
source-code licence does not carry over to its weights.

| Model | Code | Weights | Commercial use | Redistribution |
| --- | --- | --- | --- | --- |
| `vocals-hq-001` — Mel-Band RoFormer (Kim Vocal 2) | MIT | **MIT** | Permitted | Permitted |
| `standard-stems-001` — Hybrid Transformer Demucs (htdemucs v4) | MIT | **No formal licence designated — research and personal use only** | **Not permitted** | **Not permitted** |

The Demucs weights have no licence document. The only statement with standing is
the author's, on `facebookresearch/demucs` issue #327 (2022-05-23), that the
weights are not covered by the MIT licence and "are provided only for scientific
purposes". Straticate records that verbatim rather than summarising it into a
licence identifier it does not have, shows it in the app before you install, and
never redistributes the file — it downloads it from the publisher's own server.

**If you intend to use Straticate commercially, check each model's weights
licence before installing it.** The application being MIT does not make the
models so.

[0.1.0]: https://github.com/stevenpickles/straticate/releases/tag/v0.1.0
[0.2.0]: https://github.com/stevenpickles/straticate/releases/tag/v0.2.0
[0.3.0]: https://github.com/stevenpickles/straticate/releases/tag/v0.3.0
