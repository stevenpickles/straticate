import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import {
  WIDE_STEREO_NOTE,
  WIDE_STEREO_SUGGESTION_ENABLED,
  WIDE_STEREO_SUGGESTION_HOLD_REASON,
  WideStereoNote,
} from './WideStereoNote'
import type { AnalysisState } from '../state/appState'

const wide: AnalysisState = {
  status: 'loaded',
  analysis: { l_r_correlation: 0.229, wide_stereo: true },
}

const ordinary: AnalysisState = {
  status: 'loaded',
  analysis: { l_r_correlation: 0.86, wide_stereo: false },
}

function note() {
  return screen.queryByRole('note')
}

describe('WideStereoNote, with the suggestion forced on', () => {
  it('renders the note for a wide-stereo measurement', () => {
    render(<WideStereoNote analysis={wide} enabled />)
    expect(note()).toHaveTextContent(/unusually independent/i)
  })

  it('renders nothing for an ordinary stereo measurement', () => {
    render(<WideStereoNote analysis={ordinary} enabled />)
    expect(note()).toBeNull()
  })

  it.each<AnalysisState>([
    { status: 'idle' },
    { status: 'loading' },
    { status: 'failed' },
  ])('renders nothing while the measurement is $status', (analysis) => {
    render(<WideStereoNote analysis={analysis} enabled />)
    expect(note()).toBeNull()
  })

  it('offers no controls: it suggests, and cannot apply', () => {
    render(<WideStereoNote analysis={wide} enabled />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryAllByRole('radio')).toHaveLength(0)
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })
})

describe('WideStereoNote wording', () => {
  it('makes no quality promise', () => {
    // The same line `api/jobs.ts`'s picker notes are held to, and for the same
    // reason: features 041 and 062 both measured the stems reconstructing the
    // mixture at +0.999 whether the mix is folded or not, so a promise of
    // better separation is one the app cannot keep.
    expect(WIDE_STEREO_NOTE).not.toMatch(/improve|better|best|fix/i)
  })

  it('says a stem *may* come out near-silent, not that it will', () => {
    expect(WIDE_STEREO_NOTE).toMatch(/may come out near-silent/i)
    expect(WIDE_STEREO_NOTE).not.toMatch(/will come out|always/i)
  })

  it('describes what happens to the audio without naming a control', () => {
    expect(WIDE_STEREO_NOTE).toMatch(/mixing left and right together/i)
    expect(WIDE_STEREO_NOTE).toMatch(/centring just the low end/i)
    // The picker's labels are the picker's; naming them here would go stale the
    // next time the choices change, as they did when 062 added `mono_bass`.
    expect(WIDE_STEREO_NOTE).not.toMatch(
      /fold to mono|keep stereo|centre the low end\b/i,
    )
  })
})

describe('the suggestion hold', () => {
  it('is off, and says which measurement is missing', () => {
    // This is the pin. A future diff that flips the flag has to change this
    // test, which is exactly the moment someone should be asked whether the
    // false-positive measurement in
    // docs/features/063-wide-stereo-detection.md has actually been run.
    expect(WIDE_STEREO_SUGGESTION_ENABLED).toBe(false)
    expect(WIDE_STEREO_SUGGESTION_HOLD_REASON).toMatch(/false-positive/i)
    expect(WIDE_STEREO_SUGGESTION_HOLD_REASON).toMatch(
      /ordinary modern tracks/i,
    )
  })

  it('keeps the note off screen for a wide measurement by default', () => {
    render(<WideStereoNote analysis={wide} />)
    expect(note()).toBeNull()
    expect(screen.queryByText(WIDE_STEREO_NOTE)).toBeNull()
  })
})
