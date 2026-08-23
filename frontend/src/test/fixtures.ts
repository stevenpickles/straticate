/** Shared contract-shaped fixtures for tests. */
import type { AudioFile } from '../api/types'

/** A representative backend-registered audio file. */
export const sampleAudioFile: AudioFile = {
  id: '01SAMPLEAUDIOULID0000000000',
  filename: 'Midnight Train.flac',
  size_bytes: 44771328,
  uploaded_at: '2026-08-23T12:00:00Z',
  metadata: {
    duration_seconds: 227.4,
    container: 'flac',
    codec: 'flac',
    channels: 2,
    sample_rate_hz: 44100,
    bit_depth: 24,
    bit_rate_bps: 1411000,
  },
}
