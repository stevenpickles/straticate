import type { Model } from '../api/types'
import {
  attributionFallback,
  describeLicensing,
  permissionLabel,
  severityLabel,
} from '../licensing'
import { installationOf } from './useModelInstallation'
import './ModelLicence.css'

/** Props of {@link ModelLicence}. */
export interface ModelLicenceProps {
  /** The model whose terms are being shown. */
  readonly model: Model
  /**
   * Render tighter, for the configure step, where the block sits beside a
   * choice rather than in a catalog. **Only the layout changes** — every
   * permission, every notice and the attribution are shown either way, because
   * the compact placement is the one that comes *before* the download.
   */
  readonly compact?: boolean
}

/** DOM-safe suffix for this model's region label. */
function regionLabel(model: Model): string {
  return `Licensing for ${model.display_name}`
}

/**
 * What a model's `licensing` block actually says — the surface feature 025
 * served and nothing rendered until feature 037.
 *
 * It shows the **code licence and the weights licence separately, always**.
 * They are different things and routinely differ: a model whose code is MIT
 * may ship weights under CC-BY-NC, or under a sentence on a model card that
 * names no licence at all — the case feature 027 is blocked on. Collapsing
 * them into one "Licence: MIT" line would tell a user the download is free to
 * use on the strength of a fact about the *code*.
 *
 * Nothing here is ever left blank, and nothing is ever inferred:
 *
 * - a permission the manifest does not state reads "Not stated", never
 *   "Permitted" and never "Not permitted" (the contract is explicit that
 *   `null` means undeclared);
 * - a weights licence stated in words is rendered in full and marked as
 *   something to read, rather than being shown as though it were an
 *   identifier;
 * - an attribution is only described as unnecessary when a weights licence was
 *   actually declared — a model with no stated terms may well require a credit
 *   nobody has written down;
 * - the badge says at most "Terms declared". This component does not read
 *   licence texts, so it never calls anything permissive, free or open.
 *
 * A **built-in** model that declares nothing is the one quiet case: it has no
 * artifact, so nothing is fetched from a third party and there is no separate
 * weights licence to state. It says that in one line instead of warning about
 * terms that do not exist.
 */
export function ModelLicence({ model, compact = false }: ModelLicenceProps) {
  const summary = describeLicensing(model.licensing)
  const requiresDownload = installationOf(model)?.requires_download ?? false
  const className = `model-licence${compact ? ' model-licence-compact' : ''}`

  if (!summary.declared && !requiresDownload) {
    return (
      <section className={className} aria-label={regionLabel(model)}>
        <p className="model-licence-builtin">
          Built in — no weights are downloaded for this model, so it carries no
          separate weights licence.
        </p>
      </section>
    )
  }

  const rows: { label: string; value: string; modifier?: string }[] = [
    { label: 'Code licence', value: summary.code.text },
    {
      label: 'Weights licence',
      value: summary.weights.text,
      modifier:
        summary.weights.kind === 'unstated'
          ? 'model-licence-unstated'
          : undefined,
    },
    {
      label: 'Commercial use',
      value: permissionLabel(summary.commercialUse),
      modifier:
        summary.commercialUse === 'not-permitted'
          ? 'model-licence-refused'
          : summary.commercialUse === 'unstated'
            ? 'model-licence-unstated'
            : undefined,
    },
    {
      label: 'Redistribution',
      value: permissionLabel(summary.redistribution),
      modifier:
        summary.redistribution === 'not-permitted'
          ? 'model-licence-refused'
          : summary.redistribution === 'unstated'
            ? 'model-licence-unstated'
            : undefined,
    },
  ]

  return (
    <section className={className} aria-label={regionLabel(model)}>
      <p className={`model-licence-badge model-licence-${summary.severity}`}>
        {severityLabel(summary.severity)}
      </p>

      <dl className="model-licence-terms">
        {rows.map((row) => (
          <div className="model-licence-term" key={row.label}>
            <dt className="model-licence-label">{row.label}</dt>
            <dd
              className={`model-licence-value${row.modifier === undefined ? '' : ` ${row.modifier}`}`}
            >
              {row.value}
              {row.label === 'Weights licence' &&
                summary.weights.kind === 'informal' && (
                  <span className="model-licence-aside">
                    {' '}
                    (stated in words, not as a named licence)
                  </span>
                )}
            </dd>
          </div>
        ))}
      </dl>

      <div className="model-licence-attribution">
        <p className="model-licence-label">Attribution</p>
        {summary.attribution === null ? (
          <p className="model-licence-value model-licence-unstated">
            {attributionFallback(summary)}
          </p>
        ) : (
          <p className="model-licence-credit">{summary.attribution}</p>
        )}
      </div>

      {summary.notices.map((notice) => (
        <p
          className={`model-licence-notice model-licence-notice-${notice.kind}`}
          role="note"
          key={notice.kind + notice.message}
        >
          {notice.message}
        </p>
      ))}
    </section>
  )
}
