import { deleteAudio } from '../api/audio'
import type { AudioFile } from '../api/types'
import {
  formatAudioFormat,
  formatBitDepth,
  formatBitRate,
  formatChannels,
  formatDuration,
  formatFileSize,
  formatSampleRate,
} from '../format'
import { useAppDispatch } from '../state/appState'

/** One label/value pair of the metadata definition list. */
interface SummaryField {
  readonly label: string
  readonly value: string
}

/**
 * Build the metadata rows for an {@link AudioFile}. Rows whose underlying
 * value is absent (bit depth and bit rate are nullable for lossy formats)
 * are omitted rather than rendered empty.
 */
function summaryFields(file: AudioFile): SummaryField[] {
  const { metadata } = file
  const bitDepth = formatBitDepth(metadata.bit_depth)
  const bitRate = formatBitRate(metadata.bit_rate_bps)
  return [
    { label: 'Duration', value: formatDuration(metadata.duration_seconds) },
    {
      label: 'Format',
      value: formatAudioFormat(metadata.container, metadata.codec),
    },
    { label: 'Channels', value: formatChannels(metadata.channels) },
    { label: 'Sample Rate', value: formatSampleRate(metadata.sample_rate_hz) },
    ...(bitDepth === null ? [] : [{ label: 'Bit Depth', value: bitDepth }]),
    ...(bitRate === null ? [] : [{ label: 'Bit Rate', value: bitRate }]),
    { label: 'Size', value: formatFileSize(file.size_bytes) },
  ]
}

/** Props for {@link AudioSummary}. */
export interface AudioSummaryProps {
  /** The backend-registered file whose metadata is summarised. */
  file: AudioFile
}

/**
 * Summary of the uploaded audio file: its filename, prominently, followed by
 * a definition list of the probed metadata (duration, format, channels,
 * sample rate, bit depth, bit rate, size). Lets the user confirm they picked
 * the right track before configuring separation.
 *
 * "Choose a different file" clears the upload, returns the workflow to the
 * `select` phase, and best-effort deletes the uploaded file on the backend —
 * a failed delete is logged but never blocks the UI reset.
 *
 * Must be rendered under an `AppStateProvider`.
 */
export function AudioSummary({ file }: AudioSummaryProps) {
  const dispatch = useAppDispatch()

  const chooseDifferentFile = () => {
    // Reset first: the UI must return to file selection regardless of
    // whether the backend cleanup succeeds.
    dispatch({ type: 'upload/reset' })
    deleteAudio(file.id).catch((error: unknown) => {
      console.warn('Failed to delete the uploaded audio file', error)
    })
  }

  return (
    <section className="audio-summary" aria-label="Uploaded file">
      <h2 className="audio-summary-filename">{file.filename}</h2>
      <dl className="audio-summary-fields">
        {summaryFields(file).map((field) => (
          <div className="audio-summary-field" key={field.label}>
            <dt className="audio-summary-label">{field.label}</dt>
            <dd className="audio-summary-value">{field.value}</dd>
          </div>
        ))}
      </dl>
      <button
        type="button"
        className="audio-summary-change"
        onClick={chooseDifferentFile}
      >
        Choose a different file
      </button>
    </section>
  )
}
