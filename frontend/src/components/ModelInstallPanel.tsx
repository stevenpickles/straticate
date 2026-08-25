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
 *
 * **The live installation state outranks a `model_weights_missing` job
 * refusal.** That refusal is a hint that the record was stale, and the hook
 * clears it as soon as a read supersedes it; here, a download in flight
 * suppresses it outright. Otherwise a user who pressed Start, was refused, and
 * then installed would watch an 870 MiB download behind a stale error, with no
 * progress, no byte counts and a live "Install model" button beside it.
 */
export function ModelInstallPanel({ installation }: ModelInstallPanelProps) {
  const { model, error, installing, install, refresh, weightsMissingMessage } =
    installation
  const block = installationOf(model)
  const hint =
    weightsMissingMessage === null || weightsMissingMessage === ''
      ? null
      : weightsMissingMessage

  // A model that needs no weights is never presented as something to install.
  if (block !== null && !block.requires_download && hint === null) {
    return null
  }

  // Nothing has been read yet: say nothing rather than flash a panel that a
  // built-in model would not have shown at all.
  if (block === null && error === null && hint === null) {
    return null
  }

  const state = block?.state ?? 'available'
  const name = model?.display_name ?? 'This quality tier'

  // Precedence, newest fact first. A download in flight and a failure both
  // *are* the live state and say everything the hint would; a record claiming
  // `installed` while the backend has just refused a job for missing weights
  // is the one the hint is about, so there the hint wins.
  const downloading = state === 'downloading'
  const failed = state === 'failed'
  const showHint = hint !== null && !downloading && !failed
  const installed = state === 'installed' && !showHint
  const available = !downloading && !failed && !installed

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

  // Never beside a running download — a second `POST /install` only earns a
  // `model_busy` for a transfer that is going perfectly well. With nothing
  // read at all there is no honest install to offer either; a job refused for
  // missing weights is itself the invitation.
  const canInstall =
    !downloading && !installed && (block !== null || hint !== null)
  const buttonLabel = failed ? 'Retry install' : 'Install model'

  // A failed *request* — a read that did not answer, or an install that was
  // refused — always comes with the one control that can clear it. Without
  // this, a read that fails mid-download leaves a frozen bar, an error line and
  // nothing to press: the poll only resumes when a record lands.
  const requestFailureMessage =
    error === null
      ? null
      : block === null
        ? 'Could not check whether the model weights are installed.'
        : error.message

  return (
    <section className="model-install" aria-label="Model weights">
      {showHint && (
        <p className="model-install-error" role="alert">
          {hint}
        </p>
      )}

      {available && block !== null && (
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

      {requestFailureMessage !== null && (
        <div className="model-install-row">
          <p className="model-install-error" role="alert">
            {requestFailureMessage}
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
