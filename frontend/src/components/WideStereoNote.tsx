/**
 * The wide-stereo suggestion (feature 063) — implemented, and **held off**.
 *
 * Feature 041 measured a real 1968 mix whose `bass` stem came out effectively
 * silent because its channels are near-independent, wrote down the signal, the
 * threshold and the exact terms a suggestion built on them may use, and then
 * left detection out because it belonged to files 041 did not own. Feature 063
 * built it. This component is the "what it must say" row of that handoff, and
 * `WIDE_STEREO_SUGGESTION_ENABLED` is why it does not say it yet.
 */

import type { AnalysisState } from '../state/appState'

/**
 * Whether the wide-stereo suggestion is shown at all. **Currently `false`.**
 *
 * The unmet precondition is named in {@link WIDE_STEREO_SUGGESTION_HOLD_REASON}
 * and it is a measurement, not an opinion: feature 041 explicitly asked a
 * detection feature to establish its own false-positive rate, because every
 * number 041 published came from **one** record. Until that measurement exists,
 * turning this on would put a sentence about someone's recording on screen with
 * no evidence about how often it is wrong — which is the same failure feature
 * 032 exists to prevent, one layer up.
 *
 * The backend endpoint ships anyway, and the asymmetry is deliberate: it asserts
 * a *measurement* (this file's correlation is 0.23), which is true whatever the
 * false-positive rate turns out to be. This note asserts a *judgement* (your
 * recording is one of the ones that needs attention), which is not.
 *
 * **Flipping this is the whole of the follow-up**, once the protocol in
 * `docs/features/063-wide-stereo-detection.md` has been run and recorded: no
 * other code changes, and the component's tests already cover it forced true.
 */
export const WIDE_STEREO_SUGGESTION_ENABLED = false

/**
 * Why {@link WIDE_STEREO_SUGGESTION_ENABLED} is `false`, in one sentence.
 *
 * Exported, rather than left as a comment, so that a diff which flips the flag
 * has to touch this too — and so a test can pin both together. See
 * `docs/features/063-wide-stereo-detection.md` for the protocol.
 */
export const WIDE_STEREO_SUGGESTION_HOLD_REASON =
  'Held pending the false-positive measurement: at least ten ordinary modern ' +
  'tracks, supplied by the user and never committed, must all measure at or ' +
  'above the threshold before this suggestion is shown to anyone.'

/**
 * What the suggestion says, when it is shown.
 *
 * Every clause is constrained by feature 041's handoff, and the constraints are
 * worth stating because they read like ordinary caution and are not:
 *
 * - **"may"**, not "will": the measurement is of the *input*, and 041's
 *   evidence that such an input loses a stem is one track.
 * - **no quality claim** — not "better", not "improves", not "fixes". 041 and
 *   062 both measured the four stems reconstructing the mixture at +0.999
 *   whether the mix is folded or not, so nothing is gained overall; a
 *   near-silent stem becomes usable because the low end is *reassigned*. A unit
 *   test holds that line with a regular expression, the same one
 *   `api/jobs.ts`'s notes are held to.
 * - **control-agnostic**: it describes what would be done to the audio, not
 *   which radio button does it. The picker's own labels are the picker's; a
 *   sentence that named them would go stale the moment the choices changed, and
 *   they already have once (feature 062 added `mono_bass`).
 * - **it applies nothing.** Rendering this dispatches no action; the note is a
 *   `role="note"`, not a button, and the user's selection is untouched.
 */
export const WIDE_STEREO_NOTE =
  'The left and right channels of this recording are unusually independent, ' +
  'which some older stereo mixes are. On a recording like this one of the ' +
  'stems — often the bass — may come out near-silent. Mixing left and right ' +
  'together, or centring just the low end, recovers it. Neither otherwise ' +
  'changes how well the parts are told apart.'

/** Props for {@link WideStereoNote}. */
export interface WideStereoNoteProps {
  /** The upload's stereo measurement, from `AppState.analysis`. */
  readonly analysis: AnalysisState
  /**
   * Whether the suggestion may render at all. **Defaults to
   * {@link WIDE_STEREO_SUGGESTION_ENABLED} and nothing in the application ever
   * passes it** — it exists so the tests can exercise the note that ships
   * disabled, which is the only way the held code is covered rather than merely
   * written. A test also pins that the default is `false`, so passing `true`
   * here cannot quietly become the shipped behaviour.
   */
  readonly enabled?: boolean
}

/**
 * A note about the uploaded recording's stereo image, shown inside the Stereo
 * fieldset when the backend measured it as wide — and only then.
 *
 * Renders `null` in every other case, including while the measurement is in
 * flight and when it failed. That is the `useModelCatalog` precedent: an
 * enrichment never gates and never explains its own absence, because a user who
 * did not need it must not be shown a failure they cannot act on.
 *
 * **It suggests and never applies** (feature 041). It dispatches nothing,
 * selects nothing, and has no controls.
 */
export function WideStereoNote({
  analysis,
  enabled = WIDE_STEREO_SUGGESTION_ENABLED,
}: WideStereoNoteProps) {
  if (!enabled) {
    return null
  }
  if (analysis.status !== 'loaded' || !analysis.analysis.wide_stereo) {
    return null
  }
  return (
    <p className="separation-option-note wide-stereo-note" role="note">
      {WIDE_STEREO_NOTE}
    </p>
  )
}
