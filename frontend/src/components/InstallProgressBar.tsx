/** Props of {@link InstallProgressBar}. */
export interface InstallProgressBarProps {
  /** Whole percentage complete, or `null` for a transfer of unknown length. */
  readonly percent: number | null
}

/**
 * The download bar itself, shared by the configure step's install panel and
 * the model library's cards so the two cannot drift apart in what they show
 * or in how they announce it.
 *
 * The accessible name is the same in both places on purpose: "Model download
 * progress" is what it is, wherever it is mounted.
 */
export function InstallProgressBar({ percent }: InstallProgressBarProps) {
  return (
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
        style={percent === null ? undefined : { width: `${String(percent)}%` }}
      />
    </div>
  )
}
