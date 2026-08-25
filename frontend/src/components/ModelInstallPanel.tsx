import { formatFileSize } from '../format'
import {
  installationOf,
  type ModelInstallationHandle,
} from './useModelInstallation'
import './ModelInstallPanel.css'

/** Props of {@link ModelInstallPanel}. */
export interface ModelInstallPanelProps {
  /** The watched model, from `useModelInstallation`. */
  readonly installation: ModelInstallationHandle
  /**
   * The message of a `model_weights_missing` answer to `POST /jobs`, if the
   * last attempt to start a separation got one. Weights can vanish between the
   * check and the job, so this is rendered as the actionable state — with the
   * install offered — rather than as a raw error the user cannot act on.
   */
  readonly weightsMissingMessage?: string | null
}

/** Clamp a `0..1` fraction to a whole percentage. */
function toPercent(fraction: number): number {
  if (!Number.isFinite(fraction)) {
    return 0
  }
  return Math.min(100, Math.max(0, Math.round(fraction * 100)))
}

/**
 * What the configure step says about a quality tier whose model weights are
 * not on disk — and the button that puts them there.
 *
 * Straticate ships no weights (ARCHITECTURE.md §9) and, since feature 032,
 * offers no development fixture as a quality tier, so on a fresh checkout the
 * preselected tier is backed by an 870 MiB download. Before this panel the user
 * pressed "Start separation" and got a bare `model_weights_missing` with no way
 * forward.
 *
 * It renders **nothing at all** for a model that needs no download
 * (`requires_download: false` — every built-in separator), and nothing before
 * the first read answers. It never blocks the rest of the configure step:
 * modes and tiers stay selectable while a download runs, and the download runs
 * on the backend, so it survives anything the user does here short of removing
 * the weights.
 *
 * Progress is read from `installation.progress` / `downloaded_bytes` — real
 * bytes from the backend, polled once a second (see `useModelInstallation`),
 * never an animation standing in for work.
 */
export function ModelInstallPanel({
  installation,
  weightsMissingMessage = null,
}: ModelInstallPanelProps) {
  const { model, error, installing, install, refresh } = installation
  const block = installationOf(model)
  const weightsMissing =
    weightsMissingMessage !== null && weightsMissingMessage !== ''

  // A model that needs no weights is never presented as something to install.
  if (block !== null && !block.requires_download && !weightsMissing) {
    return null
  }

  // Nothing has been read yet: say nothing rather than flash a panel that a
  // built-in model would not have shown at all.
  if (block === null && error === null && !weightsMissing) {
    return null
  }

  const state = block?.state ?? 'available'
  const name = model?.display_name ?? 'This quality tier'
  const downloading = state === 'downloading' && !weightsMissing
  const installed = state === 'installed' && !weightsMissing
  const failed = state === 'failed' && !weightsMissing

  const receivedBytes = block?.downloaded_bytes ?? null
  const totalBytes = block?.total_bytes ?? null
  // `progress` is the backend's own figure; the byte counts are the fallback
  // for a transfer whose total is known but whose fraction has not been
  // computed yet. Neither is a timer.
  const fraction =
    block?.progress ??
    (receivedBytes !== null && totalBytes !== null && totalBytes > 0
      ? receivedBytes / totalBytes
      : null)
  const percent = fraction === null ? null : toPercent(fraction)
  const received = receivedBytes === null ? null : formatFileSize(receivedBytes)
  const size = totalBytes === null ? null : formatFileSize(totalBytes)

  // With nothing read and nothing missing there is no honest install to offer;
  // a `model_weights_missing` from the backend is itself the invitation.
  const canInstall = downloading
    ? false
    : weightsMissing || (block !== null && !installed)
  const buttonLabel = failed ? 'Retry install' : 'Install model'

  return (
    <section className="model-install" aria-label="Model weights">
      {weightsMissing && (
        <p className="model-install-error" role="alert">
          {weightsMissingMessage}
        </p>
      )}

      {!weightsMissing && block === null && error !== null && (
        <div className="model-install-row">
          <p className="model-install-note">
            Could not check whether the model weights are installed.
          </p>
          <button
            type="button"
            className="model-install-secondary"
            onClick={refresh}
          >
            Try again
          </button>
        </div>
      )}

      {block !== null && state === 'available' && !weightsMissing && (
        <p className="model-install-note">
          {name} needs its model weights before it can separate anything
          {size === null ? '' : ` — a ${size} download`}.
        </p>
      )}

      {downloading && (
        <>
          <div
            className={`progress-track${percent === null ? ' progress-indeterminate' : ''}`}
            role="progressbar"
            aria-label="Model download progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent ?? undefined}
          >
            <div
              className="progress-fill"
              style={
                percent === null ? undefined : { width: `${String(percent)}%` }
              }
            />
          </div>
          <p className="model-install-note">
            {percent === null
              ? `Downloading the model weights for ${name}…`
              : `Downloading the model weights — ${String(percent)}%`}
            {received !== null && size !== null
              ? ` (${received} of ${size})`
              : ''}
          </p>
          <p className="model-install-hint">
            The download runs on the server; you can keep choosing options while
            it finishes.
          </p>
        </>
      )}

      {installed && (
        <p className="model-install-note model-install-ready">
          Model weights installed{size === null ? '' : ` (${size})`}.
        </p>
      )}

      {failed && (
        <p className="model-install-error" role="alert">
          {block?.error?.message ??
            'The model weights could not be installed. Please try again.'}
        </p>
      )}

      {error !== null && block !== null && (
        <p className="model-install-error" role="alert">
          {error.message}
        </p>
      )}

      {canInstall && (
        <button
          type="button"
          className="model-install-start"
          disabled={installing}
          aria-busy={installing}
          onClick={install}
        >
          {buttonLabel}
        </button>
      )}
    </section>
  )
}
