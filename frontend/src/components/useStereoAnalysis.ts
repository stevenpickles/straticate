/**
 * Fire-and-forget read of the uploaded audio's stereo measurement (feature 063).
 *
 * `GET /api/v1/audio/{id}/analysis` is an **enrichment**: it is requested once
 * per upload, nothing waits for it, and a failure is silent. The precedent is
 * `useModelCatalog`, whose failed read leaves the quality tiers unannotated
 * rather than blocking the step — and the rule behind both is that a control the
 * user came here to use must not depend on a request they did not ask for.
 *
 * **Only a stereo upload is measured.** A single-channel file has no image to
 * correlate, the backend answers `{null, false}` without decoding anything, and
 * asking would be a round trip whose answer is known from metadata the client
 * already holds. The same check keeps the mono case out of the network log,
 * which is what a test asserts.
 *
 * It is a hook rather than a call beside the upload because there are **two**
 * ways an upload becomes current — `DropZone` finishing one, and `SessionGate`
 * restoring one after a reload (feature 033) — and a measurement fired from only
 * the first would be missing for exactly the users who reloaded. Keyed on the
 * audio ID, one request per ID, whichever way it arrived.
 */

import { useEffect, useRef } from 'react'
import { getAudioAnalysis } from '../api/audio'
import { useAppDispatch, useAppState } from '../state/appState'

/**
 * Request the current upload's stereo measurement once, into `AppState`.
 *
 * Reads the result from `useAppState().analysis`; this hook returns nothing
 * because it exists for its effect, and the state it writes is shared with
 * whatever else wants to read it.
 *
 * Must be used under an `AppStateProvider`.
 */
export function useStereoAnalysis(): void {
  const { upload } = useAppState()
  const dispatch = useAppDispatch()

  const uploaded = upload.status === 'uploaded' ? upload.file : null
  const audioId = uploaded?.id ?? null
  const isStereo = (uploaded?.metadata.channels ?? 0) >= 2

  // The ID this hook has already asked about. A ref rather than the state slice
  // because it must flip *synchronously*: two renders can happen before the
  // dispatched `analysis/requested` comes back around, and React's strict mode
  // runs every effect twice on purpose.
  const requestedFor = useRef<string | null>(null)

  useEffect(() => {
    if (audioId === null || !isStereo || requestedFor.current === audioId) {
      return
    }
    requestedFor.current = audioId
    dispatch({ type: 'analysis/requested' })
    getAudioAnalysis(audioId)
      .then((analysis) => {
        // Staleness guard (review): unreachable today — SeparationOptions
        // unmounts before a different upload can become current — but one
        // line of insurance against a future in-place replace-audio
        // affordance clobbering a newer upload's state with an older answer.
        if (requestedFor.current === audioId) {
          dispatch({ type: 'analysis/loaded', analysis })
        }
      })
      .catch(() => {
        // Deliberately swallowed. Nothing on screen depends on this, so there
        // is nothing to report and nothing to retry.
        if (requestedFor.current === audioId) {
          dispatch({ type: 'analysis/failed' })
        }
      })
  }, [audioId, isStereo, dispatch])
}
