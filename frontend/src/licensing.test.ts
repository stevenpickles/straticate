import { describe, expect, it } from 'vitest'
import {
  NOT_STATED,
  attributionFallback,
  describeLicensing,
  licenceTerm,
  permissionLabel,
  permissionState,
  severityLabel,
} from './licensing'

describe('licenceTerm', () => {
  it('treats a whitespace-free SPDX-shaped token as a named licence', () => {
    for (const id of ['MIT', 'Apache-2.0', 'CC-BY-NC-SA-4.0', 'GPL-3.0-only']) {
      expect(licenceTerm(id)).toEqual({ kind: 'named', text: id })
    }
  })

  it('treats a statement in words as informal and keeps it verbatim', () => {
    const statement =
      'Non-commercial research use only; contact the author for anything else.'
    expect(licenceTerm(statement)).toEqual({
      kind: 'informal',
      text: statement,
    })
  })

  it('classifies "MIT License" as informal rather than as the identifier MIT', () => {
    // Erring this way costs a sentence to read. Erring the other way would let
    // a paragraph of conditions be skimmed as though it were an identifier.
    expect(licenceTerm('MIT License').kind).toBe('informal')
  })

  it('is not fooled by a very long unspaced string', () => {
    expect(licenceTerm('a'.repeat(200)).kind).toBe('informal')
  })

  it('reports null, undefined and blank as explicitly not stated', () => {
    for (const value of [null, undefined, '', '   ']) {
      expect(licenceTerm(value)).toEqual({ kind: 'unstated', text: NOT_STATED })
    }
  })

  it('trims surrounding whitespace before classifying', () => {
    expect(licenceTerm('  MIT  ')).toEqual({ kind: 'named', text: 'MIT' })
  })
})

describe('permissionState', () => {
  it('never turns silence into a decision', () => {
    expect(permissionState(true)).toBe('permitted')
    expect(permissionState(false)).toBe('not-permitted')
    expect(permissionState(null)).toBe('unstated')
    expect(permissionState(undefined)).toBe('unstated')
  })

  it('labels an unstated permission as such, not as a refusal', () => {
    expect(permissionLabel('permitted')).toBe('Permitted')
    expect(permissionLabel('not-permitted')).toBe('Not permitted')
    expect(permissionLabel('unstated')).toBe(NOT_STATED)
  })
})

describe('describeLicensing with nothing declared', () => {
  it('says so for a null block rather than rendering blanks', () => {
    const summary = describeLicensing(null)
    expect(summary.declared).toBe(false)
    expect(summary.code.text).toBe(NOT_STATED)
    expect(summary.weights.text).toBe(NOT_STATED)
    expect(summary.commercialUse).toBe('unstated')
    expect(summary.redistribution).toBe('unstated')
    expect(summary.attribution).toBeNull()
    expect(summary.severity).toBe('unknown')
    expect(summary.notices).toHaveLength(1)
    expect(summary.notices[0]?.kind).toBe('unknown')
    expect(summary.notices[0]?.message).toMatch(/no licence terms at all/i)
  })

  it('treats a block whose every field is null the same way', () => {
    expect(
      describeLicensing({
        code_license: null,
        weights_license: null,
        redistribution_permitted: null,
        commercial_use_permitted: null,
        attribution: null,
      }).declared,
    ).toBe(false)
  })

  it('does not claim an attribution is unnecessary when nothing is declared', () => {
    expect(attributionFallback(describeLicensing(null))).toBe(NOT_STATED)
  })
})

describe('describeLicensing when the code is permissive and the weights are not', () => {
  it('never lets an MIT code licence stand in for silent weights terms', () => {
    // Feature 027's blocker exactly: UVR licenses its *code* as MIT and no
    // party with standing has ever stated the weights' terms.
    const summary = describeLicensing({
      code_license: 'MIT',
      weights_license: null,
      redistribution_permitted: null,
      commercial_use_permitted: null,
      attribution: null,
    })

    expect(summary.declared).toBe(true)
    expect(summary.code).toEqual({ kind: 'named', text: 'MIT' })
    expect(summary.weights.kind).toBe('unstated')
    expect(summary.severity).toBe('unknown')
    const notice = summary.notices.find((entry) => entry.kind === 'unknown')
    expect(notice?.message).toContain('MIT')
    expect(notice?.message).toMatch(/does not cover the weights/i)
  })

  it('flags weights that are licensed differently from the code', () => {
    const summary = describeLicensing({
      code_license: 'MIT',
      weights_license: 'CC-BY-NC-4.0',
      redistribution_permitted: null,
      commercial_use_permitted: null,
      attribution: null,
    })

    expect(summary.notices.some((entry) => entry.kind === 'differs')).toBe(true)
    expect(summary.severity).toBe('attention')
  })

  it('says nothing about a difference when both licences agree', () => {
    const summary = describeLicensing({
      code_license: 'MIT',
      weights_license: 'MIT',
      redistribution_permitted: true,
      commercial_use_permitted: true,
      attribution: 'Weights by Someone',
    })

    expect(summary.notices).toHaveLength(0)
    expect(summary.severity).toBe('declared')
    // Never "permissive", "free" or "open": this module reads declarations,
    // it does not interpret licence texts.
    expect(severityLabel(summary.severity)).toBe('Terms declared')
  })
})

describe('describeLicensing with restrictive or informal weights terms', () => {
  it('reports a refused commercial use as a restriction, not as silence', () => {
    const summary = describeLicensing({
      code_license: 'MIT',
      weights_license: 'CC-BY-NC-SA-4.0',
      redistribution_permitted: true,
      commercial_use_permitted: false,
      attribution: 'Open-Unmix (UMXL) by Inria/sigsep',
    })

    expect(summary.severity).toBe('restricted')
    expect(
      summary.notices.some(
        (entry) =>
          entry.kind === 'restricted' && /commercial use/i.test(entry.message),
      ),
    ).toBe(true)
    expect(severityLabel(summary.severity)).toBe('Restricted use')
  })

  it('reports a refused redistribution too, and both together', () => {
    const summary = describeLicensing({
      code_license: 'MIT',
      weights_license: 'Proprietary',
      redistribution_permitted: false,
      commercial_use_permitted: false,
      attribution: null,
    })

    const restricted = summary.notices.filter(
      (entry) => entry.kind === 'restricted',
    )
    expect(restricted).toHaveLength(2)
    expect(restricted[1]?.message).toMatch(/redistributed/i)
  })

  it('keeps an informally stated weights licence readable and flagged', () => {
    const statement =
      'Free for personal use. Ask the author before using the output commercially.'
    const summary = describeLicensing({
      code_license: 'MIT',
      weights_license: statement,
      redistribution_permitted: null,
      commercial_use_permitted: null,
      attribution: null,
    })

    expect(summary.weights).toEqual({ kind: 'informal', text: statement })
    expect(summary.notices.some((entry) => entry.kind === 'informal')).toBe(
      true,
    )
    expect(summary.severity).toBe('attention')
  })

  it('ranks a stated refusal above silence when both apply', () => {
    const summary = describeLicensing({
      code_license: 'MIT',
      weights_license: null,
      redistribution_permitted: null,
      commercial_use_permitted: false,
      attribution: null,
    })

    expect(summary.severity).toBe('restricted')
    // Neither caution is lost: the badge picks one word, the notices keep both.
    expect(summary.notices.map((entry) => entry.kind)).toEqual([
      'unknown',
      'restricted',
    ])
  })
})

describe('attribution', () => {
  it('carries the required credit verbatim', () => {
    const credit =
      'Weights: Kim Vocal 2 (Mel-Band RoFormer) by Kimberley Jensen.'
    expect(
      describeLicensing({
        code_license: 'MIT',
        weights_license: 'MIT',
        redistribution_permitted: true,
        commercial_use_permitted: true,
        attribution: credit,
      }).attribution,
    ).toBe(credit)
  })

  it('says a credit is unnecessary only when the weights licence is declared', () => {
    const declared = describeLicensing({
      code_license: 'MIT',
      weights_license: 'MIT',
      redistribution_permitted: true,
      commercial_use_permitted: true,
      attribution: null,
    })
    expect(attributionFallback(declared)).toBe('None required')

    const silent = describeLicensing({
      code_license: 'MIT',
      weights_license: null,
      redistribution_permitted: null,
      commercial_use_permitted: null,
      attribution: null,
    })
    expect(attributionFallback(silent)).toBe(NOT_STATED)
  })
})
