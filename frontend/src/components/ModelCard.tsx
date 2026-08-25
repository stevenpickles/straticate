import { useState } from 'react'
import type { Model } from '../api/types'
import { formatFileSize, formatMemorySize, formatSampleRate } from '../format'
import { DiskCostNotice } from './DiskCostNotice'
import { InstallProgressBar } from './InstallProgressBar'
import { installPercent } from './installProgress'
import { ModelLicence } from './ModelLicence'
import { installationOf, useModelInstallation } from './useModelInstallation'
import './ModelCard.css'

/** Props of {@link ModelCard}. */
export interface ModelCardProps {
  /** The catalogued model, as `GET /models` served it. */
  readonly model: Model
}

/** What the state pill says, per installation state. */
const STATE_LABELS: Record<string, string> = {
  available: 'Not installed',
  downloading: 'Downloading',
  installed: 'Installed',
  failed: 'Install failed',
}

/** A memory requirement in mebibytes, as a sentence fragment. */
function memoryValue(mebibytes: number | null | undefined): string | null {
  if (mebibytes === null || mebibytes === undefined) {
    return null
  }
  // A declared zero is a statement — "this model needs no GPU memory" — and
  // reads as nonsense rendered through a byte formatter.
  return mebibytes === 0 ? 'None' : formatMemorySize(mebibytes)
}

/** The compute backends a model declares support for, in declaration order. */
function supportedBackends(model: Model): string[] {
  return Object.entries(model.capabilities)
    .filter(([, supported]) => supported)
    .map(([backend]) => backend)
}

/**
 * One catalogued model in the library: what it is, what it needs, what its
 * terms are, and every action that can be taken on its weights.
 *
 * **The install machinery is feature 035's, reused rather than re-implemented.**
 * Each card calls `useModelInstallation` for its own model, so the reads, the
 * one-second poll and its exit conditions, the sequence-numbered responses and
 * the per-model request guards are the same code the configure step runs —
 * there is no second implementation to drift. The catalog record is what the
 * card renders until that hook's first read answers, so a row is never blank
 * and never flashes.
 *
 * **Cancel and remove are one request with two meanings.** `DELETE
 * /models/{id}/weights` cancels a running install *and* deletes installed
 * weights — feature 025 built it that way deliberately, because the outcome of
 * cancelling is exactly "this model has no weights", and because it is the
 * only escape from a transfer that will not finish. That is a good API and a
 * terrible button. So the card never shows one ambiguous control: while a
 * download runs it offers **Cancel download**, and says the partial file is
 * discarded; once weights are installed it offers **Remove weights**, and asks
 * for confirmation first, because that one throws away a download the user
 * waited for.
 */
export function ModelCard({ model }: ModelCardProps) {
  const handle = useModelInstallation(model.id)
  const [confirmingRemove, setConfirmingRemove] = useState(false)

  // The live record once it has been read, the catalog's own until then. Both
  // describe the same model, so there is no window in which the card is empty.
  const record = handle.model ?? model
  const block = installationOf(record)
  const requiresDownload = block?.requires_download ?? false
  const state = block?.state ?? 'installed'
  const totalBytes = block?.total_bytes ?? null
  const receivedBytes = block?.downloaded_bytes ?? null
  const percent = installPercent(block)
  const size = totalBytes === null ? null : formatFileSize(totalBytes)
  const received = receivedBytes === null ? null : formatFileSize(receivedBytes)
  const busy = handle.installing || handle.removing

  const stateLabel = requiresDownload
    ? (STATE_LABELS[state] ?? state)
    : 'Built in'

  const requirements = [
    {
      label: 'Recommended VRAM',
      value: memoryValue(model.requirements?.recommended_vram_mb),
    },
    {
      label: 'Minimum VRAM',
      value: memoryValue(model.requirements?.minimum_vram_mb),
    },
    {
      label: 'Minimum RAM',
      value: memoryValue(model.requirements?.minimum_ram_mb),
    },
  ].filter((row) => row.value !== null)

  const backends = supportedBackends(model)

  const facts = [
    { label: 'Separation mode', value: model.separation_mode },
    { label: 'Quality tier', value: model.quality_tier ?? 'balanced' },
    { label: 'Stems', value: model.stems.join(', ') },
    { label: 'Architecture', value: `${model.architecture} ${model.version}` },
    { label: 'Sample rate', value: formatSampleRate(model.sample_rate) },
    {
      label: 'Runs on',
      value: backends.length === 0 ? 'Not declared' : backends.join(', '),
    },
    {
      label: 'Download size',
      value: requiresDownload ? (size ?? 'Not published') : 'No download',
    },
  ]

  return (
    <article className="model-card" aria-label={model.display_name}>
      <header className="model-card-head">
        <h3 className="model-card-name">{model.display_name}</h3>
        <p
          className={`model-card-state model-card-state-${requiresDownload ? state : 'builtin'}`}
        >
          {stateLabel}
        </p>
      </header>

      <p className="model-card-id">{model.id}</p>

      {model.development_only && (
        <p className="model-card-fixture" role="note">
          Development fixture — this entry exists to exercise the application
          and does not perform real separation. A default server does not offer
          it at all.
        </p>
      )}

      <dl className="model-card-facts">
        {facts.map((fact) => (
          <div className="model-card-fact" key={fact.label}>
            <dt className="model-card-fact-label">{fact.label}</dt>
            <dd className="model-card-fact-value">{fact.value}</dd>
          </div>
        ))}
      </dl>

      <div className="model-card-requirements">
        <p className="model-card-fact-label">Hardware</p>
        {requirements.length === 0 ? (
          <p className="model-card-muted">
            No hardware requirements are declared for this model.
          </p>
        ) : (
          <ul className="model-card-requirement-list">
            {requirements.map((row) => (
              <li className="model-card-requirement" key={row.label}>
                {row.label}: {row.value}
              </li>
            ))}
          </ul>
        )}
        <p className="model-card-muted">
          Advisory only — nothing is refused for failing one.
        </p>
      </div>

      <ModelLicence model={record} />

      <div className="model-card-actions">
        {!requiresDownload && (
          <p className="model-card-muted">
            These weights ship with Straticate. There is nothing to download and
            nothing to remove.
          </p>
        )}

        {requiresDownload && state === 'downloading' && (
          <>
            <InstallProgressBar percent={percent} />
            <p className="model-card-progress">
              {percent === null
                ? 'Downloading the weights…'
                : `Downloading — ${String(percent)}%`}
              {received !== null && size !== null
                ? ` (${received} of ${size})`
                : ''}
            </p>
            <p className="model-card-muted">
              Cancelling stops the transfer and deletes the partly downloaded
              file. Nothing is kept, so installing again starts from the
              beginning.
            </p>
            <button
              type="button"
              className="model-card-danger"
              disabled={handle.removing}
              aria-busy={handle.removing}
              onClick={handle.remove}
            >
              Cancel download
            </button>
          </>
        )}

        {requiresDownload && state === 'failed' && (
          <p className="model-card-error" role="alert">
            {block?.error?.message ??
              'The last install attempt failed. Nothing was written to disk.'}
          </p>
        )}

        {requiresDownload && (state === 'available' || state === 'failed') && (
          <>
            <DiskCostNotice totalBytes={totalBytes} />
            <button
              type="button"
              className="model-card-primary"
              disabled={busy}
              aria-busy={handle.installing}
              onClick={handle.install}
            >
              {state === 'failed' ? 'Retry install' : 'Install'}
            </button>
          </>
        )}

        {requiresDownload && state === 'installed' && !confirmingRemove && (
          <>
            <p className="model-card-installed">
              Installed{size === null ? '' : ` — ${size} on disk`}.
            </p>
            <button
              type="button"
              className="model-card-danger"
              disabled={busy}
              onClick={() => {
                setConfirmingRemove(true)
              }}
            >
              Remove weights
            </button>
          </>
        )}

        {requiresDownload && state === 'installed' && confirmingRemove && (
          <div
            className="model-card-confirm"
            role="group"
            aria-label="Confirm removal"
          >
            <p className="model-card-confirm-question">
              Delete the{size === null ? '' : ` ${size} of`} weights for{' '}
              {model.display_name}? Separations using this model will not run
              until it is installed again, and installing again downloads the
              whole artifact.
            </p>
            <div className="model-card-confirm-buttons">
              <button
                type="button"
                className="model-card-danger"
                disabled={busy}
                aria-busy={handle.removing}
                onClick={() => {
                  setConfirmingRemove(false)
                  handle.remove()
                }}
              >
                Delete the weights
              </button>
              <button
                type="button"
                className="model-card-secondary"
                onClick={() => {
                  setConfirmingRemove(false)
                }}
              >
                Keep them
              </button>
            </div>
          </div>
        )}

        {handle.error !== null && (
          <div className="model-card-failure">
            <p className="model-card-error" role="alert">
              {handle.error.message}
            </p>
            <button
              type="button"
              className="model-card-secondary"
              onClick={handle.refresh}
            >
              Try again
            </button>
          </div>
        )}
      </div>
    </article>
  )
}
