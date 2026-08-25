/**
 * Reading a model's `licensing` block honestly.
 *
 * `Model.licensing` has been served since feature 025 — code licence, weights
 * licence, whether redistribution and commercial use are permitted, and the
 * attribution the licence requires — and was rendered nowhere until feature
 * 037. It is the one thing a user should read **before** committing to a
 * multi-hundred-megabyte download, because that is the only moment the terms
 * can still change the decision.
 *
 * Every field of `ModelLicensing` is optional, and the contract is explicit
 * that `null` means **"not declared", never "not permitted"** — and, just as
 * importantly, never "permitted". This module turns that block into something
 * a component can render without ever inventing a permission:
 *
 * - **Weights terms are never folded into code terms.** A model whose *code*
 *   is MIT may ship weights under CC-BY-NC, or under a sentence on a model
 *   card that names no licence at all (feature 027 is blocked on exactly that
 *   case). The two licences are separate rows, always, and a difference
 *   between them is called out.
 * - **Prose is prose.** An SPDX identifier is whitespace-free by construction,
 *   so anything containing whitespace is a statement in words, not a named
 *   licence. It is rendered verbatim and flagged as something to read rather
 *   than skim — the safe direction to be wrong in.
 * - **Nothing is ever described as permissive.** This module does not know
 *   what any licence *says*; it reports what the manifest *declares*. A
 *   summary with everything declared is labelled "Terms declared", not "Free
 *   to use".
 *
 * Pure functions, no React and no DOM: `ModelLicence.tsx` renders what these
 * return, and both the model library and the configure step render that.
 */

import type { ModelLicensing } from './api/types'

/**
 * A declared permission: granted, refused, or simply never stated.
 *
 * `unstated` is its own value rather than a `false`, because the contract says
 * a null permission means the manifest is silent — which is neither a grant
 * nor a refusal, and must never be rendered as either.
 */
export type PermissionState = 'permitted' | 'not-permitted' | 'unstated'

/**
 * How a licence was stated: as an identifier, in words, or not at all.
 *
 * `informal` is not a judgement about the terms — an informal statement can be
 * more generous than a named licence. It is a statement about *reading*: the
 * text has to be read in full, because no identifier stands in for it.
 */
export type LicenceKind = 'named' | 'informal' | 'unstated'

/** One declared licence, ready to render. */
export interface LicenceTerm {
  /** How it was stated. */
  readonly kind: LicenceKind
  /** The identifier, the statement verbatim, or an explicit "Not stated". */
  readonly text: string
}

/** Why a summary needs reading before an install. */
export type LicenceNoticeKind =
  'unknown' | 'restricted' | 'informal' | 'differs'

/** One sentence a user should read before installing a model's weights. */
export interface LicenceNotice {
  /** What kind of caution this is. */
  readonly kind: LicenceNoticeKind
  /** The sentence itself. */
  readonly message: string
}

/**
 * How much attention a model's terms need, at a glance.
 *
 * Deliberately never says "permissive": this module does not interpret licence
 * texts, so the best it can honestly report is that the terms were declared.
 */
export type LicensingSeverity =
  'restricted' | 'unknown' | 'attention' | 'declared'

/**
 * What {@link describeLicensing} needs to know beyond the licensing block.
 *
 * The block itself says nothing about whether the model *has* weights to
 * fetch, and the cautions differ entirely: "no weights licence is declared" is
 * the most important thing this module can say about a third-party download,
 * and is nonsense about a built-in separator, which downloads nothing from
 * anybody.
 */
export interface LicensingContext {
  /**
   * Whether this model's weights are fetched from a third party
   * (`installation.requires_download`). A model with no artifact is installed
   * by definition (feature 025) and has no separate weights to be licensed.
   */
  readonly weightsAreDownloaded: boolean
}

/** The default: assume a model's weights are a download, which is the risky case. */
const DOWNLOADED: LicensingContext = { weightsAreDownloaded: true }

/** A model's licensing block, resolved into what a UI may state about it. */
export interface LicensingSummary {
  /** Whether the manifest declared anything at all. */
  readonly declared: boolean
  /** Whether the weights this describes are fetched from a third party. */
  readonly weightsAreDownloaded: boolean
  /** Licence of the implementation code. */
  readonly code: LicenceTerm
  /** Licence of the weights — the one that governs the download. */
  readonly weights: LicenceTerm
  /** Whether commercial use is permitted. */
  readonly commercialUse: PermissionState
  /** Whether the weights may be redistributed. */
  readonly redistribution: PermissionState
  /** The attribution the licence requires, verbatim, or `null`. */
  readonly attribution: string | null
  /** Cautions, most consequential first. */
  readonly notices: readonly LicenceNotice[]
  /** One-word reading of {@link notices}, for a badge. */
  readonly severity: LicensingSeverity
}

/** What a licence row shows when the manifest declared nothing. */
export const NOT_STATED = 'Not stated'

/**
 * Longest string still treated as a possible identifier. SPDX IDs are short
 * (`CC-BY-NC-SA-4.0` is fifteen characters); anything longer is prose that
 * happens to contain no space.
 */
const MAX_IDENTIFIER_LENGTH = 48

/** SPDX-shaped: one whitespace-free token of identifier characters. */
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9.+-]*$/

/** Trim a nullable manifest string to `null` when it carries nothing. */
function text(value: string | null | undefined): string | null {
  const trimmed = (value ?? '').trim()
  return trimmed === '' ? null : trimmed
}

/**
 * Classify one declared licence.
 *
 * A whitespace-free SPDX-shaped token is a **named** licence; anything else —
 * "Non-commercial research use only", "see the model card", a sentence — is an
 * **informal** statement that must be read rather than recognised. Getting
 * this wrong in the informal direction costs a user one extra sentence to
 * read; getting it wrong in the named direction would let a paragraph of
 * conditions be skimmed as though it were an identifier.
 */
export function licenceTerm(value: string | null | undefined): LicenceTerm {
  const stated = text(value)
  if (stated === null) {
    return { kind: 'unstated', text: NOT_STATED }
  }
  const named =
    stated.length <= MAX_IDENTIFIER_LENGTH && IDENTIFIER.test(stated)
  return { kind: named ? 'named' : 'informal', text: stated }
}

/** Resolve a nullable manifest boolean into a {@link PermissionState}. */
export function permissionState(
  value: boolean | null | undefined,
): PermissionState {
  if (value === true) {
    return 'permitted'
  }
  if (value === false) {
    return 'not-permitted'
  }
  return 'unstated'
}

/** Human-readable label for a {@link PermissionState}. */
export function permissionLabel(state: PermissionState): string {
  switch (state) {
    case 'permitted':
      return 'Permitted'
    case 'not-permitted':
      return 'Not permitted'
    case 'unstated':
      return NOT_STATED
  }
}

/** Badge text for a {@link LicensingSeverity}. */
export function severityLabel(severity: LicensingSeverity): string {
  switch (severity) {
    case 'restricted':
      return 'Restricted use'
    case 'unknown':
      return 'Terms not stated'
    case 'attention':
      return 'Read the terms'
    case 'declared':
      return 'Terms declared'
  }
}

/**
 * Build the cautions a summary carries, most consequential first.
 *
 * The two `unknown` cautions are about **a download whose terms nobody has
 * stated**, so they are only raised for a model that actually fetches its
 * weights. Warning that "the code licence does not cover the weights" about a
 * built-in separator would be inventing a risk: there is no artifact, nothing
 * is fetched from a third party, and there are no separate weights to license.
 * Everything else — a refusal, an informal statement, a difference between the
 * two licences — is just as true either way and is still raised.
 */
function buildNotices(
  declared: boolean,
  code: LicenceTerm,
  weights: LicenceTerm,
  commercialUse: PermissionState,
  redistribution: PermissionState,
  weightsAreDownloaded: boolean,
): LicenceNotice[] {
  const notices: LicenceNotice[] = []

  if (weightsAreDownloaded) {
    if (!declared) {
      notices.push({
        kind: 'unknown',
        message:
          'This model declares no licence terms at all. Nothing here says how its weights may be used — treat them as unlicensed until the publisher states otherwise.',
      })
    } else if (weights.kind === 'unstated') {
      notices.push({
        kind: 'unknown',
        message:
          code.kind === 'unstated'
            ? 'No weights licence is declared. Treat the terms as unknown until the publisher states them.'
            : `No weights licence is declared. The code licence (${code.text}) does not cover the weights, so their terms are unknown.`,
      })
    }
  }

  if (commercialUse === 'not-permitted') {
    notices.push({
      kind: 'restricted',
      message:
        'Commercial use of these weights is not permitted. They are cleared for personal use only.',
    })
  }
  if (redistribution === 'not-permitted') {
    notices.push({
      kind: 'restricted',
      message:
        'These weights may not be redistributed. Straticate downloads them from their publisher rather than shipping them.',
    })
  }

  if (weights.kind === 'informal') {
    notices.push({
      kind: 'informal',
      message:
        'The weights terms are stated in words rather than as a named licence. Read them in full before installing.',
    })
  }

  if (
    weights.kind !== 'unstated' &&
    code.kind !== 'unstated' &&
    weights.text !== code.text
  ) {
    notices.push({
      kind: 'differs',
      message:
        'The weights are licensed separately from the code. The code licence says nothing about what you may do with the download.',
    })
  }

  return notices
}

/**
 * Pick the one word a badge shows.
 *
 * A stated refusal outranks silence: "you may not" is a fact to obey, while
 * "not stated" is a caution to investigate — and both keep their own notice
 * either way, so the badge never hides one behind the other.
 */
function severityOf(notices: readonly LicenceNotice[]): LicensingSeverity {
  if (notices.some((notice) => notice.kind === 'restricted')) {
    return 'restricted'
  }
  if (notices.some((notice) => notice.kind === 'unknown')) {
    return 'unknown'
  }
  return notices.length > 0 ? 'attention' : 'declared'
}

/**
 * Resolve a model's `licensing` block into what may honestly be rendered
 * about it. A missing block is a summary that says so, never a blank.
 */
export function describeLicensing(
  licensing: ModelLicensing | null | undefined,
  context: LicensingContext = DOWNLOADED,
): LicensingSummary {
  const block = licensing ?? {}
  const code = licenceTerm(block.code_license)
  const weights = licenceTerm(block.weights_license)
  const commercialUse = permissionState(block.commercial_use_permitted)
  const redistribution = permissionState(block.redistribution_permitted)
  const attribution = text(block.attribution)
  const declared =
    code.kind !== 'unstated' ||
    weights.kind !== 'unstated' ||
    commercialUse !== 'unstated' ||
    redistribution !== 'unstated' ||
    attribution !== null

  const notices = buildNotices(
    declared,
    code,
    weights,
    commercialUse,
    redistribution,
    context.weightsAreDownloaded,
  )

  return {
    declared,
    weightsAreDownloaded: context.weightsAreDownloaded,
    code,
    weights,
    commercialUse,
    redistribution,
    attribution,
    notices,
    severity: severityOf(notices),
  }
}

/**
 * What the attribution row says when there is no attribution string.
 *
 * "None required" is a claim about the licence, so it may only be made when a
 * licence was actually declared. With nothing declared, the honest answer is
 * that nothing is known — a model whose terms are unstated may well require a
 * credit nobody has written down.
 *
 * A model that downloads no weights is the exception: there is no third party
 * whose credit could be owed, so silence there really does mean none.
 */
export function attributionFallback(summary: LicensingSummary): string {
  if (!summary.weightsAreDownloaded) {
    return 'None required'
  }
  return summary.weights.kind === 'unstated' ? NOT_STATED : 'None required'
}
