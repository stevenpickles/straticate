import type { Model } from '../api/types'
import { MODEL_LIBRARY_ID } from './Header'
import { installationOf } from './useModelInstallation'
import { ModelCard } from './ModelCard'
import { useModelCatalog } from './useModelCatalog'
import './ModelLibrary.css'

/** Props of {@link ModelLibrary}. */
export interface ModelLibraryProps {
  /** Return to the workflow. */
  readonly onClose: () => void
}

/** How many of the catalogued models have their weights on disk. */
function installedCount(models: readonly Model[]): number {
  return models.filter((model) => installationOf(model)?.state === 'installed')
    .length
}

/** `n item` / `n items`, without a library. */
function plural(count: number, noun: string): string {
  return `${String(count)} ${noun}${count === 1 ? '' : 's'}`
}

/**
 * The model library: every catalogued model, what it needs, what its licence
 * says, and every action that can be taken on its weights.
 *
 * This is the screen features 025, 032 and 035 each deferred to. It is
 * deliberately **beside** the workflow rather than inside it: the workflow's
 * five phases are about one audio file, and managing models is not a step of
 * separating one. Opening it hides the workspace without unmounting it, so a
 * decoded stem player, a running job's progress and the upload behind them are
 * exactly where they were when it closes.
 *
 * Everything rendered here comes from `GET /models`; nothing about the catalog
 * is known to the client in advance (AGENTS.md principle 6). A server that
 * hides development fixtures does not list them, so this shows what the user
 * is actually offered — and a server that opts them back in gets them labelled
 * for what they are, which is the gap feature 032 left open.
 */
export function ModelLibrary({ onClose }: ModelLibraryProps) {
  const catalog = useModelCatalog()
  const { models, status } = catalog

  return (
    <section
      className="model-library"
      id={MODEL_LIBRARY_ID}
      aria-label="Model library"
    >
      <header className="model-library-head">
        <div>
          <h2 className="model-library-title">Model library</h2>
          <p className="model-library-subtitle">
            Straticate ships no model weights. Each model below is downloaded
            from its publisher on request, so its licence and its size are worth
            reading before you install it.
          </p>
        </div>
        <button type="button" className="model-library-close" onClick={onClose}>
          Back to workflow
        </button>
      </header>

      {status === 'loading' && (
        <p className="model-library-note">Loading the model catalog…</p>
      )}

      {status === 'error' && (
        <div className="model-library-failure">
          <p className="model-library-error" role="alert">
            {catalog.error?.message ?? 'The model catalog could not be read.'}
          </p>
          <button
            type="button"
            className="model-library-retry"
            onClick={catalog.refresh}
          >
            Try again
          </button>
        </div>
      )}

      {status === 'loaded' && models.length === 0 && (
        <p className="model-library-note">
          This server offers no models at all. Check the catalog it was started
          with.
        </p>
      )}

      {status === 'loaded' && models.length > 0 && (
        <>
          <p className="model-library-summary" role="status">
            {plural(models.length, 'model')} catalogued ·{' '}
            {String(installedCount(models))} installed
          </p>
          <ul className="model-library-list">
            {models.map((model) => (
              <li className="model-library-item" key={model.id}>
                <ModelCard model={model} />
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
