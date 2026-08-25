import { describe, expect, it } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { ModelLicence } from './ModelLicence'
import {
  modelLicensed,
  sampleBuiltInModel,
  sampleInstallableModel,
  samplePermissiveLicensing,
  sampleRestrictiveLicensing,
  sampleSilentWeightsLicensing,
} from '../test/fixtures'
import type { Model } from '../api/types'

/** The licensing region for `model`. */
function renderLicence(model: Model, compact = false) {
  render(<ModelLicence model={model} compact={compact} />)
  return screen.getByRole('region', {
    name: `Licensing for ${model.display_name}`,
  })
}

/** The value rendered under a term label. */
function termValue(region: HTMLElement, label: string): string {
  const term = within(region)
    .getByText(label, { selector: '.model-licence-label' })
    .closest('.model-licence-term, .model-licence-attribution')
  expect(term, `the "${label}" row is rendered`).not.toBeNull()
  return (
    term?.querySelector('.model-licence-value, .model-licence-credit')
      ?.textContent ?? ''
  )
}

describe('ModelLicence with permissive terms', () => {
  it('shows the code and weights licences as separate rows', () => {
    const region = renderLicence(modelLicensed(samplePermissiveLicensing))

    expect(termValue(region, 'Code licence')).toBe('MIT')
    expect(termValue(region, 'Weights licence')).toBe('MIT')
    expect(termValue(region, 'Commercial use')).toBe('Permitted')
    expect(termValue(region, 'Redistribution')).toBe('Permitted')
  })

  it('renders the attribution the licence requires, verbatim', () => {
    const region = renderLicence(modelLicensed(samplePermissiveLicensing))

    expect(termValue(region, 'Attribution')).toBe(
      samplePermissiveLicensing.attribution,
    )
  })

  it('says only that the terms are declared — never that they are permissive', () => {
    const region = renderLicence(modelLicensed(samplePermissiveLicensing))

    expect(within(region).getByText('Terms declared')).toBeInTheDocument()
    expect(region.textContent).not.toMatch(
      /permissive|free to use|open source/i,
    )
    expect(within(region).queryAllByRole('note')).toHaveLength(0)
  })
})

describe('ModelLicence when the weights are more restrictive than the code', () => {
  it('never lets an MIT code licence imply anything about silent weights', () => {
    const region = renderLicence(modelLicensed(sampleSilentWeightsLicensing))

    expect(termValue(region, 'Code licence')).toBe('MIT')
    expect(termValue(region, 'Weights licence')).toBe('Not stated')
    expect(within(region).getByText('Terms not stated')).toBeInTheDocument()
    expect(
      within(region)
        .getAllByRole('note')
        .map((note) => note.textContent)
        .join(' '),
    ).toMatch(/does not cover the weights/i)
  })

  it('does not claim a credit is unnecessary when nothing was declared', () => {
    const region = renderLicence(modelLicensed(sampleSilentWeightsLicensing))

    expect(termValue(region, 'Attribution')).toBe('Not stated')
  })

  it('renders an informally stated weights licence in full and flags it', () => {
    const region = renderLicence(modelLicensed(sampleRestrictiveLicensing))
    const weights = termValue(region, 'Weights licence')

    expect(weights).toContain(sampleRestrictiveLicensing.weights_license)
    expect(weights).toContain('stated in words, not as a named licence')
  })

  it('states a refused permission as a refusal, and badges the model restricted', () => {
    const region = renderLicence(modelLicensed(sampleRestrictiveLicensing))

    expect(termValue(region, 'Commercial use')).toBe('Not permitted')
    expect(termValue(region, 'Redistribution')).toBe('Not permitted')
    expect(within(region).getByText('Restricted use')).toBeInTheDocument()
    expect(
      within(region)
        .getAllByRole('note')
        .map((note) => note.textContent)
        .join(' '),
    ).toMatch(/commercial use of these weights is not permitted/i)
  })

  it('says a permission is not stated rather than leaving it blank', () => {
    const region = renderLicence(
      modelLicensed({
        code_license: 'MIT',
        weights_license: 'CC-BY-4.0',
        redistribution_permitted: null,
        commercial_use_permitted: null,
        attribution: 'Credit: Someone.',
      }),
    )

    expect(termValue(region, 'Commercial use')).toBe('Not stated')
    expect(termValue(region, 'Redistribution')).toBe('Not stated')
  })

  it('points out that the weights are licensed separately from the code', () => {
    const region = renderLicence(
      modelLicensed({
        code_license: 'MIT',
        weights_license: 'CC-BY-NC-4.0',
        redistribution_permitted: null,
        commercial_use_permitted: null,
        attribution: null,
      }),
    )

    expect(
      within(region)
        .getAllByRole('note')
        .map((note) => note.textContent)
        .join(' '),
    ).toMatch(/licensed separately from the code/i)
  })
})

describe('ModelLicence with nothing declared', () => {
  it('says so for a downloadable model rather than rendering a blank block', () => {
    const region = renderLicence(modelLicensed(null))

    expect(within(region).getByText('Terms not stated')).toBeInTheDocument()
    expect(termValue(region, 'Weights licence')).toBe('Not stated')
    expect(
      within(region)
        .getAllByRole('note')
        .map((note) => note.textContent)
        .join(' '),
    ).toMatch(/no licence terms at all/i)
  })

  it('does not warn about weights terms for a model that has no weights', () => {
    // A built-in separator fetches nothing from a third party, so there is no
    // separate weights licence to be silent about.
    const region = renderLicence(sampleBuiltInModel)

    expect(region).toHaveTextContent(/no weights are downloaded/i)
    expect(within(region).queryAllByRole('note')).toHaveLength(0)
    expect(region.textContent).not.toMatch(/unlicensed/i)
  })

  it('still shows declared terms for a built-in model', () => {
    const region = renderLicence(
      modelLicensed(samplePermissiveLicensing, sampleBuiltInModel),
    )

    expect(termValue(region, 'Weights licence')).toBe('MIT')
  })
})

describe('ModelLicence compact', () => {
  it('drops nothing: the compact placement is the one before the download', () => {
    const model = modelLicensed(sampleRestrictiveLicensing)
    const region = renderLicence(model, true)

    expect(region).toHaveClass('model-licence-compact')
    expect(termValue(region, 'Code licence')).toBe('MIT')
    expect(termValue(region, 'Commercial use')).toBe('Not permitted')
    expect(termValue(region, 'Attribution')).toBe(
      sampleRestrictiveLicensing.attribution,
    )
    expect(within(region).getAllByRole('note').length).toBeGreaterThan(0)
  })

  it('is shown for a model whose weights are not installed yet', () => {
    // The terms have to be readable *before* the install, which is the only
    // moment they can still change the decision.
    expect(
      sampleInstallableModel.installation?.state,
      'the fixture is the uninstalled case',
    ).toBe('available')
    const region = renderLicence(
      modelLicensed(sampleRestrictiveLicensing, sampleInstallableModel),
      true,
    )
    expect(termValue(region, 'Weights licence')).toContain(
      'Research and personal use only',
    )
  })
})
