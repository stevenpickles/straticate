# [041] Mono fold-down for wide-stereo material

Branch: `041-mono-folddown-option`
Status: PLANNED
Dependencies: 028

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

**Folding the input to mono fixes it**, verified on the same track, same model,
same settings:

| | stereo (as released) | mono fold-down |
| --- | --- | --- |
| bass rms | −65.7 dBFS | **−32.6 dBFS** |
| bass peak | 0.0054 | **0.2377** |

33 dB, with the other stems unaffected. `htdemucs_ft` was tested and does **not**
help (its bass specialist gives −66.2 dBFS), so this is the fix that works.

## The question this feature must answer

Mono fold-down is proven, but it is not obviously the *right* control to expose:

- **Fold to mono** — simple, proven, and throws away the stereo image of every
  stem. Acceptable for stem extraction, less so if the user wanted stereo stems.
- **Narrow the stereo image** (mid/side with the side component attenuated,
  rather than removed) — keeps some width, and may recover the bass with less
  loss. **Unmeasured.** Worth measuring before choosing, because if a partial
  narrowing works it is strictly better.
- **Detect and suggest** — L/R correlation is cheap to compute at upload (the
  audio is already decoded for `ffprobe` metadata), so the app could notice a
  wide mix and offer the option rather than making the user know to look for it.
  This is the most useful version and the most work.

Whichever is chosen, it is a **preprocessing choice attached to a job**, not a
model property — so it belongs in `SeparationConfiguration`, which makes it a
shared-contract change (`schemas/`, regenerated `api.d.ts`, the frontend
configure step). Note that the current contract deliberately keeps
architecture-specific tuning out of user-facing configuration
(ARCHITECTURE.md §1); a stereo-handling choice is a *user* decision about their
audio, not an inference parameter, so it does not violate that — but say so
explicitly in the feature doc, because it looks similar.

## Out of scope

Automatic application without the user's knowledge — silently altering someone's
audio is exactly the kind of thing this project has avoided elsewhere (see the
fake-separator honesty rules and feature 032). Per-stem stereo reconstruction.
Any change to the models.

## Acceptance criteria

- [ ] Measured comparison of at least fold-to-mono against mid/side narrowing on
      the same wide-stereo material, with the numbers recorded
- [ ] The chosen control is exposed as a job configuration option, documented in
      the REST contract, with `api.d.ts` regenerated
- [ ] Default behaviour is unchanged — existing jobs separate exactly as before
- [ ] If detection is implemented, it *suggests* and never silently applies
- [ ] All gates green

## Required tests

The measurement is the evidence, and belongs in the feature doc. In CI, test the
preprocessing transform itself (a known input folds/narrows to a known output)
and that the configuration reaches the separator — not the separation quality,
which needs real weights and belongs in the integration tier.
